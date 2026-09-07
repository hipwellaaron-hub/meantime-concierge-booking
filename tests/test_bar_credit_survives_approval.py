"""The bar credit is a promise, and it lives beside the words, not inside them.

A booking's bar credit has to reach the floor: the team reads the Bar
Structure section on the night, and the credit changes how the tab is run
from the opening drink. It used to get there by being composed INTO
content["bar_structure"] by the generator.

That put a generated fragment inside a human-editable field, and made every
consumer responsible for peeling it off again. Four of them did not, and
each was its own defect (reviews of 43dc67a):

  - approving a proposal replaced the field and deleted the promise;
  - re-composing on approval could double the line, or leave a stale figure
    standing beside the live one, or strip an approved value to nothing and
    write the generator's placeholder over a person's words;
  - on a booking with a credit the regenerate screen reported the
    generator's own placeholder as a human value at risk, and suppressed
    its "would be emptied" badge on exactly the bookings carrying money;
  - the approval badge stopped matching, losing a true attribution;
  - the review panel told staff generated text was somebody's words;
  - and a kept field froze a stale figure beside a bar_credit that said
    otherwise.

Every one of those is a consumer comparing a value that has something
generated glued to the front of it. So the field now holds only the words,
content["bar_credit"] keeps the figure it already kept, and the two are
joined at the one point that renders them.
"""

import datetime as dt
import re
from decimal import Decimal

from app.models import Contact
from app.models.document import DocumentType
from app.services import beo_proposals
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import (
    bar_structure_shown,
    generate_beo_content,
    strip_bar_credit_line,
)

CREDIT_LINE = "$500 bar credit included, applied on the night."
PROPOSED = "Bar tab to $2,000, then cash bar. House wine and tap beer only."
LEGACY = CREDIT_LINE + "\n\nBar tab to $2,000."


def _booking_with_credit(db, space, credit=Decimal("500.00"), name="Bar Credit Test"):
    contact = Contact(name="Bar Credit Client", email=f"bar.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    booking.bar_credit = credit
    db.commit()
    db.refresh(booking)
    return booking


def _beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


# --- the stored field holds only the words ------------------------------------


def test_the_generator_no_longer_writes_the_credit_into_the_field(db, loft):
    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)

    assert "bar credit included" not in document.content["bar_structure"]
    assert document.content["bar_credit"] == "500.00", "the figure still travels, where it always did"


def test_the_section_still_prints_the_credit_above_the_words(db, loft):
    """What the floor sees is unchanged. That is the point of moving it."""
    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)

    shown = bar_structure_shown(document.content)
    assert shown.startswith(CREDIT_LINE)
    assert shown.endswith(document.content["bar_structure"])


def test_a_booking_with_no_credit_prints_no_line(db, loft):
    booking = _booking_with_credit(db, loft, credit=Decimal("0.00"), name="No Credit")
    document = _beo(db, booking)

    assert "bar credit" not in bar_structure_shown(document.content)


def test_the_rendered_document_shows_the_credit(db, loft, admin_client):
    """End to end through the real template, since the whole change is about
    where the credit is joined on."""
    booking = _booking_with_credit(db, loft, name="Rendered")
    document = _beo(db, booking)
    # Real bar wording: client_safe replaces the WHOLE value with a neutral
    # placeholder whenever it carries a [REVIEW] marker, so a document with
    # nothing filled in shows the client no bar section at all. That is
    # unchanged by this commit -- the old composed field carried the same
    # marker -- but it means the credit only reaches a client once somebody
    # has written the structure.
    documents_service.update_content(
        db, document, {**document.content, "bar_structure": "Bar tab to $2,000, then cash bar."},
        actor="staff:aaron@meantime.com.au",
        authored_fields=dr.PROTECTED_FIELD_NAMES, placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)
    documents_service.mark_sent(db, document, actor="staff:test")

    page = admin_client.get(f"/d/{document.access_token}")

    assert page.status_code == 200
    assert CREDIT_LINE in page.text
    assert "Bar tab to $2,000, then cash bar." in page.text


# --- documents written before the change ---------------------------------------


def test_a_legacy_document_does_not_print_the_credit_twice(db, loft):
    """Content stored before this change has the line baked in. It is
    stripped at render, so the figure appears once."""
    shown = bar_structure_shown({"bar_structure": LEGACY, "bar_credit": "500.00"})

    assert shown.count("bar credit included") == 1
    assert shown.endswith("Bar tab to $2,000.")


def test_a_legacy_stale_figure_is_superseded_by_the_live_one(db, loft):
    """The old design froze whatever figure was current when the field was
    written. Rendering from bar_credit means the document cannot state two."""
    shown = bar_structure_shown({"bar_structure": LEGACY, "bar_credit": "750.00"})

    assert shown.startswith("$750 bar credit included")
    assert "$500 bar credit" not in shown


def test_a_legacy_line_a_person_typed_above_is_still_removed():
    """The strip is MULTILINE: the old anchored version only recognised a
    line at position 0, so a person's note above it left the figure to be
    printed twice."""
    assert strip_bar_credit_line(
        "Note from Sally.\n" + CREDIT_LINE + "\nBar tab."
    ) == "Note from Sally.\nBar tab."


def test_a_sentence_somebody_wrote_is_never_mistaken_for_the_credit_line():
    theirs = "Client has $500 credit with us from the deposit -- apply to bar."
    assert strip_bar_credit_line(theirs) == theirs


# --- approval writes the approved words, and nothing else ----------------------


def test_approving_a_proposal_stores_exactly_what_was_approved(db, loft):
    """No re-compose, so nothing can double the line, strip the value to
    empty, or delete a credit line the approver approved."""
    booking = _booking_with_credit(db, loft, name="Approved")
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"] == PROPOSED
    assert bar_structure_shown(document.content).startswith(CREDIT_LINE), "and the promise still prints"


def test_a_proposal_that_echoes_the_credit_line_does_not_blank_the_field(db, loft):
    """The worst of the old design: the model echoed back the line it could
    see, the strip reduced it to nothing, and the generator's placeholder
    was written over the real bar wording while the audit trail recorded the
    credit sentence as what was approved."""
    booking = _booking_with_credit(db, loft, name="Echoed")
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": CREDIT_LINE}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"] == CREDIT_LINE, "stored verbatim, whatever it says"
    assert "[REVIEW]" not in document.content["bar_structure"]


def test_a_zero_credit_booking_keeps_an_approved_credit_sentence(db, loft):
    """The strip used the booking's own credit, so on a zero-credit booking
    it deleted a credit line the approver had deliberately approved."""
    booking = _booking_with_credit(db, loft, credit=Decimal("0.00"), name="Zero Credit")
    document = _beo(db, booking)
    theirs = CREDIT_LINE + "\n\nBar tab to $2,000."
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": theirs}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"] == theirs


def test_the_approval_badge_still_names_the_approver(db, loft):
    """Nothing is prepended, so the stored value equals applied_value and the
    comparison that drives the badge matches without a special case."""
    booking = _booking_with_credit(db, loft, name="Badge")
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")
    beo_proposals.approve_field(db, row, actor="staff:sally@meantime.com.au")
    db.refresh(document)

    fresh = {**generate_beo_content(booking), "bar_structure": "Cash bar only."}
    loss = next(x for x in dr.losses(db, document, fresh) if x.field == "bar_structure")

    assert loss.approved_note is not None
    assert "sally@meantime.com.au" in loss.approved_note


# --- the consumers that were comparing a value with a prefix on it -------------


def test_the_generated_placeholder_is_recognised_on_a_credit_booking(db, loft):
    """losses() compares the stored value against GENERATED_PLACEHOLDERS. With
    the credit glued on it never matched, so the generator's own placeholder
    was reported as a human value at risk -- and on the wizard path, which
    auto-keeps with nobody present, the client's real answer was discarded
    for it."""
    booking = _booking_with_credit(db, loft, name="Placeholder")
    document = _beo(db, booking)
    assert document.content["bar_structure"].startswith("[REVIEW]")

    fresh = {**generate_beo_content(booking), "bar_structure": "Bar tab to $2,000, agreed with the client."}
    found = [x.field for x in dr.losses(db, document, fresh)]

    assert "bar_structure" not in found, "there is nothing of anybody's to lose"


def test_would_be_emptied_is_not_suppressed_on_a_credit_booking(db, loft):
    """empties_the_field asks whether the INCOMING value is disposable. With
    the credit glued on, the incoming value was never a placeholder, so the
    screen's loudest badge was dead on exactly the bookings carrying money."""
    booking = _booking_with_credit(db, loft, name="Emptied")
    document = _beo(db, booking)
    documents_service.update_content(
        db, document, {**document.content, "bar_structure": "Bar tab, agreed with Aaron."},
        actor="staff:aaron@meantime.com.au",
        authored_fields=dr.PROTECTED_FIELD_NAMES, placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)

    fresh = generate_beo_content(booking)
    loss = next(x for x in dr.losses(db, document, fresh) if x.field == "bar_structure")

    assert loss.empties_the_field is True


def test_the_review_panel_does_not_call_generated_text_somebodys_words(db, loft):
    """review_rows.replaces_text tested whether the current value starts with
    [REVIEW]. With the credit in front of it, it never did."""
    booking = _booking_with_credit(db, loft, name="Review Panel")
    document = _beo(db, booking)
    beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )

    rows = beo_proposals.review_rows(db, booking.id, document=document)
    row = next(r for r in rows if r["field"] == "bar_structure")

    assert row["replaces_text"] is False, "nobody has written anything here"


def test_keeping_the_field_on_a_regenerate_cannot_freeze_a_figure(db, loft):
    """apply_choices copies the kept value verbatim. It used to carry the
    credit, so a keep after the credit changed wrote a document stating one
    figure in the prose and another in bar_credit."""
    booking = _booking_with_credit(db, loft, name="Kept")
    document = _beo(db, booking)
    documents_service.update_content(
        db, document, {**document.content, "bar_structure": "Bar tab, agreed with Aaron."},
        actor="staff:aaron@meantime.com.au",
        authored_fields=dr.PROTECTED_FIELD_NAMES, placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)

    booking.bar_credit = Decimal("750.00")
    db.commit()
    db.refresh(booking)
    fresh = generate_beo_content(booking)

    merged = dr.apply_choices(fresh, document, {"bar_structure"})

    assert "bar credit" not in merged["bar_structure"], "no figure is frozen into the words"
    assert merged["bar_credit"] == "750.00"
    assert bar_structure_shown(merged).startswith("$750 bar credit included")
