"""What the approval badge on the regenerate screen is allowed to claim.

The badge is the only place that screen says WHO put a value on a document.
It was made from string equality alone: if the current text matched
something approved earlier for that field, the screen credited the
approver. That is right until somebody hand-edits the draft -- approve a
sentence, edit it away, edit it back, and the badge credits the approver
for text the editor typed.

It has to mean "this exact text was approved", not "this field was
approved once".

Two things make that hard, and both are facts about the system rather than
choices here:

  - the authorship record cannot settle it. An approval records authorship
    exactly as a hand-edit does, because both run through the same writer
    (beo_proposals passes authored_fields to update_content_fields);
  - hand-edits are logged per document VERSION, never per field
    (documents.py writes field_name="<type>_version"), so nothing says
    WHICH field a person typed into.

So the rule is timing, and where timing cannot settle it the badge says
nothing. Fewer badges, no false ones.
"""

import datetime as dt

from app.models import BookingEvent
from app.models.beo_proposal import FIELD_APPROVED, BeoProposal, BeoProposalField
from app.models.document import DocumentType
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.document_generation import NO_DIETARIES, generate_beo_content

APPROVED_TEXT = "1x severe nut allergy (table 4)."
APPROVED_AT = dt.datetime(2026, 9, 3, 10, 0, tzinfo=dt.timezone.utc)


def _draft_with_approval(db, booking, *, text=APPROVED_TEXT, decided_at=APPROVED_AT, applied=None):
    """A draft whose dietaries value got there by approval -- built through
    the real models, because the defect was only visible with real rows."""
    base = generate_beo_content(booking)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**base, "dietaries": text}, actor="test"
    )
    proposal = BeoProposal(booking_id=booking.id, document_id=document.id, source="test", created_by="ai")
    db.add(proposal)
    db.flush()
    db.add(
        BeoProposalField(
            proposal_id=proposal.id,
            field="dietaries",
            proposed_value=text,
            applied_value=text if applied is None else applied,
            state=FIELD_APPROVED,
            decided_at=decided_at,
            decided_by="sally@meantime.com.au",
        )
    )
    db.commit()
    db.refresh(document)
    return base, document


def _hand_edit(db, booking, document, when):
    """A hand-edit event of the shape documents.update_content really
    writes: per document VERSION, with no field name on it."""
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="document_edited",
            field_name=f"{document.type.value}_version",
            new_value=str(document.version),
            actor="staff:aaron@meantime.com.au",
            created_at=when,
        )
    )
    db.commit()


def _dietaries_note(db, document, base):
    found = dr.losses(db, document, {**base, "dietaries": NO_DIETARIES})
    row = next(loss for loss in found if loss.field == "dietaries")
    return row.approved_note


def test_an_untouched_approval_is_still_credited(db, booking):
    """The badge's whole reason for existing: the screen can say who put
    this text here and when, rather than "something"."""
    base, document = _draft_with_approval(db, booking)

    assert _dietaries_note(db, document, base) == "approved by sally@meantime.com.au on 3 Sep 2026"


def test_a_hand_edit_after_the_approval_withdraws_the_claim(db, booking):
    """Proved with real rows before the fix: the badge still read "approved
    by sally@meantime.com.au on 3 Sep 2026" after a hand-edit that
    post-dated the approval, so the screen credited Sally for whatever the
    editor had typed. Hand-edits carry no field name, so this cannot say
    the edit missed dietaries -- and says nothing rather than guessing."""
    base, document = _draft_with_approval(db, booking)
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) is None


def test_a_hand_edit_BEFORE_the_approval_leaves_the_claim_standing(db, booking):
    """The approval is the later act, so it is still what put this text
    here. Withdrawing the badge for any hand-edit at all would empty the
    screen of the one thing it can honestly say."""
    base, document = _draft_with_approval(db, booking)
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) == "approved by sally@meantime.com.au on 3 Sep 2026"


def test_an_approval_with_no_timestamp_cannot_outrank_a_hand_edit(db, booking):
    """decided_at is nullable. With no time on the approval there is no
    ordering to reason from, so an edited draft gets no badge."""
    base, document = _draft_with_approval(db, booking, decided_at=None)
    assert _dietaries_note(db, document, base) == "approved by sally@meantime.com.au on an earlier date"

    _hand_edit(db, booking, document, dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) is None


def test_text_that_matches_no_approval_is_never_credited(db, booking):
    """The equality check still has to do its job: a value nobody approved
    carries no badge, hand-edits or not."""
    base, document = _draft_with_approval(db, booking, applied="Something else entirely.")

    assert _dietaries_note(db, document, base) is None


def test_an_approval_applied_to_a_different_version_still_counts(db, booking):
    """Approvals are per booking and per field, and a regenerate makes a new
    version. The badge answers "was this text approved", so it must not be
    scoped to the version the approval happened on -- while the hand-edit
    check IS per version, because that is how hand-edits are logged."""
    base, document = _draft_with_approval(db, booking)
    later = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**base, "dietaries": APPROVED_TEXT}, actor="test"
    )

    found = dr.losses(db, later, {**base, "dietaries": NO_DIETARIES})
    row = next(loss for loss in found if loss.field == "dietaries")
    assert row.approved_note == "approved by sally@meantime.com.au on 3 Sep 2026"


def test_a_regenerate_does_not_resurrect_a_withdrawn_badge(db, booking):
    """The hole the first version of this fix left. Hand-edits are logged
    per document VERSION, but the TEXT a badge describes outlives versions:
    a regenerate that keeps the value produces a new version with no edit
    events of its own, and the badge came back crediting the approver for
    words somebody had typed on the version before.

    Proved live before the widening: v1 correctly said None, v2 said
    "approved by sally@meantime.com.au on 5 Sep 2026" (review of
    d24aba5)."""
    base, v1 = _draft_with_approval(db, booking)
    _hand_edit(db, booking, v1, dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc))
    assert _dietaries_note(db, v1, base) is None, "withdrawn on the version that was edited"

    # a regenerate keeps the value, so the same text lands in a new version
    v2 = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**base, "dietaries": APPROVED_TEXT}, actor="staff:aaron"
    )

    assert dr.was_hand_edited(db, v2) is None, "the new version carries no edit events of its own"
    assert _dietaries_note(db, v2, base) is None, "and the badge stays withdrawn"


def test_a_hand_edit_to_the_OTHER_document_type_leaves_the_badge_alone(db, booking):
    """The widening is per booking AND per document type. An agreement
    edited by hand says nothing about who wrote the Event Order's
    dietaries, and withdrawing that badge would cost a true statement for
    no reason."""
    base, document = _draft_with_approval(db, booking)
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, {"terms_sections": []}, actor="test"
    )
    _hand_edit(db, booking, agreement, dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) == "approved by sally@meantime.com.au on 3 Sep 2026"


def test_the_LATEST_hand_edit_is_the_one_that_counts(db, booking):
    """With edits either side of the approval, only the later one settles
    it. Every other test here creates a single edit, which leaves the
    ordering of the query unobservable -- a mutation taking the OLDEST edit
    passed all of them (review of d24aba5)."""
    base, document = _draft_with_approval(db, booking)
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) is None, "the later edit is what matters"


def test_edits_only_BEFORE_the_approval_still_leave_the_badge(db, booking):
    """The mirror, so the rule is pinned from both sides rather than by one
    example: several edits, all older than the approval, change nothing."""
    base, document = _draft_with_approval(db, booking)
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))
    _hand_edit(db, booking, document, dt.datetime(2026, 9, 2, 9, 0, tzinfo=dt.timezone.utc))

    assert _dietaries_note(db, document, base) == "approved by sally@meantime.com.au on 3 Sep 2026"
