"""Propose-and-approve for the ten free-text Event Order fields.

The AI proposes; Aaron approves. Nothing here can write an Event Order on
its own, and there is no code path that applies a proposal without a
staff actor passing through approve_field / approve_all.

What the layer guarantees:

- A proposal is validated the moment it arrives (app.services.beo_rules).
  A blocked one is stored for calibration and never becomes reviewable.
- Approval applies to the booking's CURRENT Event Order draft, resolved
  at approval time, never to whatever document existed when the proposal
  was made. An Event Order that has been sent cannot be edited at all --
  that guard is documents.update_content's, and it is the same one the
  hand-edit form goes through.
- Every approval records what the field held before, what was proposed,
  and what was actually written. Approving an edited value is the signal
  the whole feature is measured on.
- A new proposal supersedes whatever was still pending on an older one,
  so the review screen only ever shows the current ask.
"""

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent
from app.models.beo_proposal import (
    FIELD_APPROVED,
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
    decided, a proposal that is not reviewable."""


def normalise_beo_field(field: str, value: str | None) -> str | None:
    """The document's own storage shape for one field value."""
    text = (value or "").strip()
    if field == "dietaries":
        return text or NO_DIETARIES
    if field in _NULLABLE_WHEN_EMPTY:
        return text or None
    return text


def current_draft_beo(db: Session, booking_id: uuid.UUID) -> Document | None:
    """The Event Order a proposal would be applied to: the current one,
    and only while it is still a draft. A sent, viewed or signed Event
    Order is out of scope by design."""
    document = documents_service.get_current(db, booking_id, DocumentType.beo)
    if document is None or document.is_legacy or document.status != DocumentStatus.draft:
        return None
    return document


def _display(field: str, value) -> str:
    if value is None:
        return ""
    return str(value)


def current_values(document: Document | None) -> dict[str, str]:
    """What the Event Order reads today, for the ten proposable fields --
    the "current" column of the review panel, and the base the RSA floor
    is checked against."""
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
        values[field] = _display(field, raw)
    return values


def pending_proposal(db: Session, booking_id: uuid.UUID) -> BeoProposal | None:
    """The one proposal a reviewer should be looking at, if any."""
    proposal = db.scalars(
        select(BeoProposal)
        .where(BeoProposal.booking_id == booking_id, BeoProposal.status == STATUS_PENDING)
        .order_by(BeoProposal.created_at.desc())
    ).first()
    return proposal if proposal is not None and proposal.is_reviewable else proposal


def _supersede_older(db: Session, booking_id: uuid.UUID, *, keep: uuid.UUID, actor: str) -> int:
    """A newer ask replaces whatever was still pending. Fields already
    approved or rejected keep their state and their audit trail."""
    superseded = 0
    older = db.scalars(
        select(BeoProposal).where(
            BeoProposal.booking_id == booking_id,
            BeoProposal.id != keep,
            BeoProposal.status == STATUS_PENDING,
        )
    ).all()
    for proposal in older:
        for field_row in proposal.fields:
            if field_row.state == FIELD_PENDING:
                field_row.state = FIELD_SUPERSEDED
                superseded += 1
        proposal.status = STATUS_SUPERSEDED
        proposal.resolved_at = dt.datetime.now(dt.timezone.utc)
    return superseded


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
    document = current_draft_beo(db, booking.id)
    proposed = {name: (value or "").strip() for name, value in (fields or {}).items()}

    effective = current_values(document)
    effective.update({k: v for k, v in proposed.items() if k in beo_rules.PROPOSABLE_FIELDS})

    result = beo_rules.validate(
        proposed,
        effective=effective,
        event_type=booking.event_type,
        event_name=booking.event_name,
        child_count=booking.child_count or 0,
    )

    proposal = BeoProposal(
        booking_id=booking.id,
        document_id=document.id if document is not None else None,
        status=STATUS_RULES_BLOCKED if result.blocked else STATUS_PENDING,
        source=source.strip()[:500],
        trigger=trigger,
        model=model,
        rule_codes=result.codes or None,
        rule_note=result.as_note() or None,
        created_by=actor,
    )
    db.add(proposal)
    db.flush()

    # Field rows are written even for a blocked proposal: what it wanted
    # to say is the calibration record. They are simply never reviewable,
    # because the proposal's own status is not pending.
    for name in beo_rules.PROPOSABLE_FIELDS:
        if name in proposed:
            db.add(
                BeoProposalField(
                    proposal_id=proposal.id,
                    field=name,
                    state=FIELD_PENDING if not result.blocked else FIELD_SUPERSEDED,
                    proposed_value=proposed[name],
                )
            )

    if not result.blocked:
        _supersede_older(db, booking.id, keep=proposal.id, actor=actor)

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


def _claim(db: Session, field_row: BeoProposalField) -> None:
    """Lock one field row and confirm it is still awaiting a decision, so
    two clicks on Approve cannot both apply."""
    db.refresh(field_row, with_for_update=True)
    if field_row.state != FIELD_PENDING:
        raise ProposalError(f"that field is already {field_row.state}")
    if field_row.proposal.status != STATUS_PENDING:
        raise ProposalError(f"this proposal is {field_row.proposal.status} and cannot be approved")


def approve_field(
    db: Session, field_row: BeoProposalField, *, actor: str, value: str | None = None
) -> Document:
    """Write one proposed field onto the current Event Order draft.

    `value` is what Aaron actually wants written: the proposed text when
    he accepted it as offered, his own wording when he edited it first.
    Both are recorded.
    """
    _claim(db, field_row)
    proposal = field_row.proposal
    document = current_draft_beo(db, proposal.booking_id)
    if document is None:
        raise ProposalError(
            "there is no Event Order draft on this booking to apply it to -- generate one first, and note that "
            "an Event Order that has already been sent cannot be edited"
        )

    applied = field_row.proposed_value if value is None else value
    content = dict(document.content)
    previous = current_values(document)[field_row.field]
    content[field_row.field] = normalise_beo_field(field_row.field, applied)
    if field_row.field == "music":
        # The merged legacy field is what the edit form clears on save;
        # leaving it behind would let it out-rank the value just approved.
        content["music_entertainment"] = None

    field_row.state = FIELD_APPROVED
    field_row.previous_value = previous
    field_row.applied_value = (applied or "").strip()
    field_row.decided_at = dt.datetime.now(dt.timezone.utc)
    field_row.decided_by = actor
    _resolve_if_complete(proposal)

    db.add(
        BookingEvent(
            booking_id=proposal.booking_id,
            event_type="beo_proposal_approved",
            field_name=field_row.field,
            old_value=previous or None,
            new_value=field_row.applied_value or None,
            actor=actor,
        )
    )
    if field_row.edited_before_approval:
        # The measure of whether the transcription is working: recorded as
        # its own event so it can be counted without diffing every row.
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
    # Commits, and refuses if the document stopped being a draft under us.
    return documents_service.update_content(db, document, content, actor=actor)


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
    one commit. `values` may carry edited wording per field, same as
    approve_field."""
    if proposal.status != STATUS_PENDING or not proposal.pending_fields:
        raise ProposalError("there is nothing pending on this proposal")
    document = current_draft_beo(db, proposal.booking_id)
    if document is None:
        raise ProposalError(
            "there is no Event Order draft on this booking to apply it to -- generate one first, and note that "
            "an Event Order that has already been sent cannot be edited"
        )

    content = dict(document.content)
    previous_values = current_values(document)
    now = dt.datetime.now(dt.timezone.utc)
    values = values or {}
    for field_row in list(proposal.pending_fields):
        _claim(db, field_row)
        applied = values.get(field_row.field)
        applied = field_row.proposed_value if applied is None else applied
        content[field_row.field] = normalise_beo_field(field_row.field, applied)
        if field_row.field == "music":
            content["music_entertainment"] = None
        field_row.state = FIELD_APPROVED
        field_row.previous_value = previous_values[field_row.field]
        field_row.applied_value = (applied or "").strip()
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
    return documents_service.update_content(db, document, content, actor=actor)


def review_rows(db: Session, booking_id: uuid.UUID) -> list[dict]:
    """What the Event Order form shows: every pending field, its proposed
    value and the value it would replace."""
    proposal = pending_proposal(db, booking_id)
    if proposal is None or not proposal.is_reviewable:
        return []
    document = current_draft_beo(db, booking_id)
    current = current_values(document)
    rows = []
    for field_row in proposal.fields:
        if field_row.state != FIELD_PENDING:
            continue
        rows.append(
            {
                "field": field_row.field,
                "label": beo_rules.FIELD_LABELS[field_row.field],
                "id": field_row.id,
                "proposed": field_row.proposed_value,
                "current": current[field_row.field],
                "replaces_text": bool(current[field_row.field].strip()),
            }
        )
    return rows
