"""Propose-and-approve for the ten free-text Event Order fields.

The AI proposes; Aaron approves. Nothing here can write an Event Order on
its own, and there is no code path that applies a proposal without a
staff actor passing through approve_field / approve_all.

What the layer guarantees:

- A proposal is validated when it arrives, and the value actually being
  written is validated AGAIN at approval (app.services.beo_rules). The
  approval screen lets Aaron edit the text first, so a propose-time-only
  gate could be walked past by pasting into the box (2026-09-06 review).
- Approval applies to the booking's CURRENT Event Order draft, resolved
  at approval time under a row lock taken BEFORE the content is read, so
  two approvals cannot silently revert one another. An Event Order that
  has been sent cannot be edited at all.
- Every approval records what the field held before, what was proposed,
  and what was actually written. Approving an edited value is the signal
  the whole feature is measured on, so the comparison is made on
  normalised line endings -- a browser rewrites a textarea's newlines,
  and that must not read as a human correction.
- One pending proposal per booking: a new ask supersedes what was still
  pending, under an advisory lock so two concurrent proposals cannot both
  survive. Superseding a field writes its own audit row, because a
  proposed allergy that nobody ever saw disappearing is exactly the thing
  the timeline has to be able to explain.
"""

import datetime as dt
import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent
from app.models.beo_proposal import (
    FIELD_APPROVED,
    FIELD_BLOCKED,
    FIELD_PENDING,
    FIELD_REJECTED,
    FIELD_SUPERSEDED,
    STATUS_PENDING,
    STATUS_RESOLVED,
    STATUS_RULES_BLOCKED,
    STATUS_SUPERSEDED,
    BeoProposal,
    BeoProposalField,
)
from app.models.document import Document, DocumentStatus, DocumentType
from app.services import beo_rules, documents as documents_service
from app.services.booking import VOIDED_STATUSES

logger = logging.getLogger(__name__)

# The document never prints a blank Dietaries section: an empty value
# reads as this sentence, so "nothing declared" is a statement rather
# than an oversight. One definition, used by the hand-edit form and by
# an approved proposal alike.
NO_DIETARIES = "No dietary requirements declared"

# Fields the document stores as None when empty, rather than "".
_NULLABLE_WHEN_EMPTY = ("music", "entertainment", "accessibility", "decorations", "onsite_contact")


class ProposalError(ValueError):
    """Something a caller can fix: no draft to apply to, a field already
    decided, a value the house rules refuse."""


def normalise_newlines(value: str | None) -> str:
    """Browsers submit a textarea's newlines as CRLF. Storing that would
    make every untouched multi-line approval read as an edit, which is the
    one number this feature exists to produce."""
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def normalise_beo_field(field: str, value: str | None) -> str | None:
    """The document's own storage shape for one field value."""
    text_value = normalise_newlines(value).strip()
    if field == "dietaries":
        return text_value or NO_DIETARIES
    if field in _NULLABLE_WHEN_EMPTY:
        return text_value or None
    return text_value


def current_draft_beo(db: Session, booking_id: uuid.UUID) -> Document | None:
    """The Event Order a proposal would be applied to: the current one,
    and only while it is still a draft. A sent, viewed or signed Event
    Order is out of scope by design."""
    document = documents_service.get_current(db, booking_id, DocumentType.beo)
    if document is None or document.is_legacy or document.status != DocumentStatus.draft:
        return None
    return document


def current_values(document: Document | None) -> dict[str, str]:
    """What the Event Order reads today, for the ten proposable fields --
    the "current" column of the review panel, and the base every
    comparison rule is judged against."""
    content = (document.content if document is not None else None) or {}
    values: dict[str, str] = {}
    for field in beo_rules.PROPOSABLE_FIELDS:
        raw = content.get(field)
        if field == "dietaries" and raw == NO_DIETARIES:
            raw = ""
        if field == "music" and not raw:
            # Older documents carried one merged music/entertainment field;
            # the edit form offers it as the music prefill, so the review
            # panel has to show the same thing or "current" would read
            # blank against a document that visibly is not.
            raw = content.get("music_entertainment")
        values[field] = "" if raw is None else str(raw)
    return values


def can_receive_proposals(booking: Booking) -> str | None:
    """Why this booking cannot take a proposal, or None if it can.

    A cancelled or dead booking is not having an event, and a linked
    child room can never have an Event Order of its own (see
    documents.create_new_version) -- proposing against either would
    store something nobody can ever review."""
    if booking.status in VOIDED_STATUSES:
        return f"this booking is {booking.status.value}, so its Event Order is not going anywhere"
    if booking.parent_booking_id is not None:
        return "this is a linked second room; the Event Order belongs to the parent booking"
    return None


def pending_proposal(db: Session, booking_id: uuid.UUID) -> BeoProposal | None:
    """The newest proposal still awaiting review, if any. Callers decide
    what to do with one that has no fields left pending (`is_reviewable`)."""
    return db.scalars(
        select(BeoProposal)
        .where(BeoProposal.booking_id == booking_id, BeoProposal.status == STATUS_PENDING)
        .order_by(BeoProposal.created_at.desc())
    ).first()


def _proposal_lock(db: Session, booking_id: uuid.UUID) -> None:
    """Serialise proposals for one booking, so "supersede whatever was
    pending, then insert" cannot interleave with itself and leave two
    pending proposals -- one of which would be invisible forever. Same
    transaction-level advisory lock the enquiry path uses."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"), {"k": f"beo_proposal:{booking_id}"})


def _supersede_older(db: Session, booking_id: uuid.UUID, *, actor: str) -> None:
    """A newer ask replaces whatever was still pending. Fields already
    approved or rejected keep their state and their audit trail; a field
    that is dropped without ever being seen gets its own audit row, so
    the timeline can explain where a proposed value went."""
    older = db.scalars(
        select(BeoProposal).where(
            BeoProposal.booking_id == booking_id, BeoProposal.status == STATUS_PENDING
        )
    ).all()
    for proposal in older:
        for field_row in proposal.fields:
            if field_row.state == FIELD_PENDING:
                field_row.state = FIELD_SUPERSEDED
                db.add(
                    BookingEvent(
                        booking_id=booking_id,
                        event_type="beo_proposal_superseded",
                        field_name=field_row.field,
                        old_value=field_row.proposed_value or None,
                        actor=actor,
                    )
                )
        proposal.status = STATUS_SUPERSEDED
        proposal.resolved_at = dt.datetime.now(dt.timezone.utc)


def _rule_context(booking: Booking) -> dict:
    return {
        "event_type": booking.event_type,
        "event_name": booking.event_name,
        "notes": booking.notes,
        "child_count": booking.child_count or 0,
    }


def propose(
    db: Session,
    booking: Booking,
    *,
    fields: dict[str, str],
    source: str,
    actor: str,
    trigger: str | None = None,
    model: str | None = None,
) -> tuple[BeoProposal, beo_rules.RuleResult]:
    """Store one proposal. Always writes a row -- a blocked proposal is
    kept for calibration, exactly as a rules-blocked draft is -- and
    returns it with the rule result so the caller can answer the AI.

    Never applies anything.
    """
    refusal = can_receive_proposals(booking)
    if refusal is not None:
        raise ProposalError(refusal)

    _proposal_lock(db, booking.id)
    document = current_draft_beo(db, booking.id)
    proposed = {name: normalise_newlines(value).strip() for name, value in (fields or {}).items()}
    current = current_values(document)

    result = beo_rules.validate(proposed, current=current, **_rule_context(booking))

    if not result.blocked:
        # Before the insert, so a partial unique index on "one pending
        # proposal per booking" is satisfied at every point.
        _supersede_older(db, booking.id, actor=actor)

    proposal = BeoProposal(
        booking_id=booking.id,
        document_id=document.id if document is not None else None,
        status=STATUS_RULES_BLOCKED if result.blocked else STATUS_PENDING,
        source=source.strip()[:500],
        trigger=trigger,
        model=model,
        rule_codes=result.codes or None,
        rule_note=result.as_note() or None,
        warning_codes=result.warning_codes or None,
        warning_note=result.warning_note() or None,
        created_by=actor,
    )
    db.add(proposal)
    db.flush()

    # Field rows are written even for a blocked proposal: what it wanted
    # to say is the calibration record. FIELD_BLOCKED keeps that readable
    # apart from a field a newer ask replaced.
    for name in beo_rules.PROPOSABLE_FIELDS:
        if name in proposed:
            db.add(
                BeoProposalField(
                    proposal_id=proposal.id,
                    field=name,
                    state=FIELD_BLOCKED if result.blocked else FIELD_PENDING,
                    proposed_value=proposed[name],
                )
            )

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="beo_proposal_created" if not result.blocked else "beo_proposal_blocked",
            field_name=",".join(sorted(proposed))[:100] or None,
            old_value=source.strip()[:500],
            new_value=",".join(result.codes) if result.blocked else None,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(proposal)
    return proposal, result


def _resolve_if_complete(proposal: BeoProposal) -> None:
    if proposal.status == STATUS_PENDING and not proposal.pending_fields:
        proposal.status = STATUS_RESOLVED
        proposal.resolved_at = dt.datetime.now(dt.timezone.utc)


def _locked_draft(db: Session, booking_id: uuid.UUID) -> Document:
    """The current Event Order draft, locked for update BEFORE its content
    is read. Reading first and locking later is a lost update: two
    approvals of different fields would each write a whole JSONB blob
    built from a stale snapshot (2026-09-06 review)."""
    document = current_draft_beo(db, booking_id)
    if document is None:
        raise ProposalError(
            "there is no Event Order draft on this booking to apply it to -- generate one first, and note "
            "that an Event Order that has already been sent cannot be edited"
        )
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ProposalError(f"this Event Order is {document.status.value} and can no longer be edited")
    if not document.is_current:
        # The draft was resolved BEFORE the lock, so between those two a
        # regenerate can supersede it -- and this row is locked by id, so
        # the lock is granted on a version that is no longer the live one.
        # Applying here would write the approval onto a document nobody
        # will ever read, and report success (proved live, 2026-09-06).
        raise ProposalError(
            "this Event Order was replaced by a newer version while you were approving -- reload the "
            "booking and review the proposal against the current Event Order"
        )
    return document


def _claim(db: Session, field_row: BeoProposalField) -> None:
    """Lock one field row and confirm it is still awaiting a decision, so
    two clicks on Approve cannot both apply."""
    db.refresh(field_row, with_for_update=True)
    if field_row.state != FIELD_PENDING:
        raise ProposalError(
            f"that field is already {field_row.state} -- someone else may have acted on it, or a newer "
            "proposal replaced it. Reload the Event Order."
        )
    if field_row.proposal.status != STATUS_PENDING:
        raise ProposalError(
            f"this proposal is {field_row.proposal.status} and cannot be approved -- reload the Event Order."
        )


def _check_on_approval(booking: Booking, changes: dict[str, str], current: dict[str, str]) -> None:
    """The house rules, on the values actually being written. The box is
    editable, so this is the check that cannot be pasted past."""
    result = beo_rules.validate(changes, current=current, **_rule_context(booking))
    if result.blocked:
        raise ProposalError(result.as_note())


def _apply(
    db: Session,
    document: Document,
    proposal: BeoProposal,
    decisions: list[tuple[BeoProposalField, str]],
    *,
    actor: str,
) -> Document:
    """Write the approved values onto the locked draft, in one merge and
    one commit, recording what each one replaced."""
    previous_values = current_values(document)
    changes: dict[str, str | None] = {}
    now = dt.datetime.now(dt.timezone.utc)
    for field_row, applied in decisions:
        changes[field_row.field] = normalise_beo_field(field_row.field, applied)
        if field_row.field == "music":
            # The merged legacy field is what the edit form clears on save;
            # leaving it behind would let it out-rank the value approved.
            changes["music_entertainment"] = None
        field_row.state = FIELD_APPROVED
        field_row.previous_value = previous_values[field_row.field]
        field_row.applied_value = normalise_newlines(applied).strip()
        field_row.decided_at = now
        field_row.decided_by = actor
        db.add(
            BookingEvent(
                booking_id=proposal.booking_id,
                event_type="beo_proposal_approved",
                field_name=field_row.field,
                old_value=previous_values[field_row.field] or None,
                new_value=field_row.applied_value or None,
                actor=actor,
            )
        )
        if field_row.edited_before_approval:
            # The measure of whether the transcription is working: its own
            # event, so it can be counted without diffing every row.
            db.add(
                BookingEvent(
                    booking_id=proposal.booking_id,
                    event_type="beo_proposal_edited",
                    field_name=field_row.field,
                    old_value=field_row.proposed_value or None,
                    new_value=field_row.applied_value or None,
                    actor=actor,
                )
            )
    _resolve_if_complete(proposal)
    return documents_service.update_content_fields(db, document, changes, actor=actor)


def approve_field(
    db: Session, field_row: BeoProposalField, *, actor: str, value: str | None = None
) -> Document:
    """Write one proposed field onto the current Event Order draft.

    `value` is what Aaron actually wants written: None (the box was not
    submitted) means the proposed text stands; his own wording means his
    wording is written. Both are recorded.
    """
    proposal = field_row.proposal
    document = _locked_draft(db, proposal.booking_id)
    _claim(db, field_row)
    applied = field_row.proposed_value if value is None else normalise_newlines(value)
    _check_on_approval(document.booking, {field_row.field: applied}, current_values(document))
    return _apply(db, document, proposal, [(field_row, applied)], actor=actor)


def reject_field(db: Session, field_row: BeoProposalField, *, actor: str) -> BeoProposalField:
    _claim(db, field_row)
    field_row.state = FIELD_REJECTED
    field_row.decided_at = dt.datetime.now(dt.timezone.utc)
    field_row.decided_by = actor
    _resolve_if_complete(field_row.proposal)
    db.add(
        BookingEvent(
            booking_id=field_row.proposal.booking_id,
            event_type="beo_proposal_rejected",
            field_name=field_row.field,
            old_value=field_row.proposed_value or None,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(field_row)
    return field_row


def approve_all(
    db: Session, proposal: BeoProposal, *, actor: str, values: dict[str, str] | None = None
) -> Document:
    """Approve every field still pending, in one write to the document and
    one commit. `values` may carry edited wording per field; a field it
    does not mention keeps what was proposed."""
    if proposal.status != STATUS_PENDING or not proposal.pending_fields:
        raise ProposalError("there is nothing pending on this proposal")
    document = _locked_draft(db, proposal.booking_id)
    values = values or {}

    decisions: list[tuple[BeoProposalField, str]] = []
    for field_row in list(proposal.pending_fields):
        _claim(db, field_row)
        submitted = values.get(field_row.field)
        decisions.append(
            (field_row, field_row.proposed_value if submitted is None else normalise_newlines(submitted))
        )
    _check_on_approval(
        document.booking, {row.field: applied for row, applied in decisions}, current_values(document)
    )
    return _apply(db, document, proposal, decisions, actor=actor)


def review_rows(db: Session, booking_id: uuid.UUID, *, document: Document | None = None) -> list[dict]:
    """What the Event Order form shows: every pending field, its proposed
    value and the value it would replace."""
    proposal = pending_proposal(db, booking_id)
    if proposal is None or not proposal.is_reviewable:
        return []
    if document is None:
        document = current_draft_beo(db, booking_id)
    current = current_values(document)
    rows = []
    for field_row in proposal.fields:
        if field_row.state != FIELD_PENDING:
            continue
        existing = current[field_row.field]
        rows.append(
            {
                "field": field_row.field,
                "label": beo_rules.FIELD_LABELS[field_row.field],
                "id": field_row.id,
                "proposed": field_row.proposed_value,
                "current": existing,
                # A generation placeholder is not content anyone wrote, so
                # overwriting it is not the risky case the warning is for.
                "replaces_text": bool(existing.strip()) and not existing.lstrip().startswith("[REVIEW]"),
            }
        )
    return rows
