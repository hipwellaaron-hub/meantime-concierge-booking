"""What a regenerate is about to destroy, named field by field.

Regenerating a document builds fresh content from the booking and replaces
the current version with it. For everything the booking computes -- the
timeline, the food order, totals, the room -- that is exactly right and is
the whole reason the button exists.

For the fields a person writes in their own words it is not. Those values
are not derivable from anything: an approved allergy note, a hand-edited
layout instruction. Regenerating discarded them silently, and silence is
the worst property that failure could have. Proved on 2026-09-06:
generate_beo_content defaults Dietaries to "No dietary requirements
declared", so one click of Regenerate replaced a declared nut allergy with
that sentence and made it version 2 -- Aaron's original incident, in a new
costume, with no human in the loop at all.

So a regenerate that would destroy a human value now stops and says
exactly what it is about to discard, and the human decides per field.
Nothing here decides for them; it only refuses to decide silently.

Scope, deliberately: only the free-text fields below. Diffing the derived
structures too would produce a screen nobody reads, and a screen nobody
reads is the silence this module exists to end.
"""

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BookingEvent, Document
from app.models.beo_proposal import FIELD_APPROVED, BeoProposal, BeoProposalField
from app.services import beo_rules
from app.services.document_generation import REVIEW

logger = logging.getLogger(__name__)

# The fields a person writes in their own words. The ten a proposal may
# touch (beo_rules.PROPOSABLE_FIELDS), plus the two free-text fields that
# predate proposals and the legacy merged music field. Asserted against
# PROPOSABLE_FIELDS by test, so the two lists cannot drift apart.
PROTECTED_TEXT_FIELDS: tuple[str, ...] = beo_rules.PROPOSABLE_FIELDS + (
    "music_entertainment",
    "internal_notes",
    "status_text",
)

FIELD_LABELS = {
    **beo_rules.FIELD_LABELS,
    "music_entertainment": "Music & entertainment",
    "internal_notes": "Internal notes (staff/kitchen)",
    "status_text": "Status text",
}

# Values that are not a loss: a generated prompt or the generated default.
# Replacing one of these with a fresh one destroys nothing a human wrote.
# The dietaries default is listed by value because that is what the
# generator emits when nothing was captured -- and it is precisely the
# sentence that overwrote a real allergy, so it must never itself count as
# something worth keeping.
_GENERATED_DEFAULTS = ("No dietary requirements declared",)


def _text(value: object) -> str:
    """One spelling for comparison. A CRLF/LF difference is not an edit --
    the same lesson as the approval box (2026-09-06 review)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _is_disposable(value: str) -> bool:
    """True when the current value holds nothing a human would miss."""
    if not value:
        return True
    if value in _GENERATED_DEFAULTS:
        return True
    return REVIEW in value


@dataclass(frozen=True)
class ContentLoss:
    """One field whose human-written value a regenerate would destroy."""

    field: str
    label: str
    current: str
    incoming: str
    # Set when this exact value can be traced to an approval, so the
    # screen can say who approved it and when rather than "something".
    approved_note: str | None = None

    @property
    def empties_the_field(self) -> bool:
        """The worst shape: real words replaced by nothing or by a
        generated default that asserts the opposite."""
        return _is_disposable(self.incoming)


def _approved_values(db: Session, booking_id) -> dict[str, list[BeoProposalField]]:
    rows = db.scalars(
        select(BeoProposalField)
        .join(BeoProposal, BeoProposalField.proposal_id == BeoProposal.id)
        .where(BeoProposal.booking_id == booking_id, BeoProposalField.state == FIELD_APPROVED)
        .order_by(BeoProposalField.decided_at)
    ).all()
    by_field: dict[str, list[BeoProposalField]] = {}
    for row in rows:
        by_field.setdefault(row.field, []).append(row)
    return by_field


def _approval_note(rows: list[BeoProposalField], current: str) -> str | None:
    for row in reversed(rows):
        if _text(row.applied_value) == current:
            # No %-d: it is a glibc extension and raises on Windows, where
            # the tests run.
            when = row.decided_at.strftime("%d %b %Y").lstrip("0") if row.decided_at else "an earlier date"
            who = row.decided_by or "staff"
            return f"approved by {who} on {when}"
    return None


def was_hand_edited(db: Session, document: Document) -> BookingEvent | None:
    """Whether THIS version carries a hand-edit. Hand-edits are recorded per
    document version, not per field, so this can say the draft was edited
    but never which field -- the screen says so rather than guessing."""
    return db.scalars(
        select(BookingEvent)
        .where(
            BookingEvent.booking_id == document.booking_id,
            BookingEvent.event_type == "document_edited",
            BookingEvent.field_name == f"{document.type.value}_version",
            BookingEvent.new_value == str(document.version),
        )
        .order_by(BookingEvent.created_at.desc())
        .limit(1)
    ).first()


def losses(db: Session, document: Document | None, fresh: dict) -> list[ContentLoss]:
    """The human-written values `fresh` would destroy, in field order.

    Empty when there is nothing to lose -- no current document, or every
    protected field either unchanged or holding only a generated
    placeholder. An empty result means a regenerate is safe to run
    straight through, which is the ordinary case.
    """
    if document is None:
        return []
    current_content = document.content or {}
    approved = _approved_values(db, document.booking_id)

    found: list[ContentLoss] = []
    for name in PROTECTED_TEXT_FIELDS:
        current = _text(current_content.get(name))
        incoming = _text(fresh.get(name))
        if current == incoming or _is_disposable(current):
            continue
        found.append(
            ContentLoss(
                field=name,
                label=FIELD_LABELS.get(name, name.replace("_", " ").capitalize()),
                current=current,
                incoming=incoming,
                approved_note=_approval_note(approved.get(name, []), current),
            )
        )
    return found


def fingerprint(found: list[ContentLoss]) -> str:
    """Identifies the exact set of losses a human was shown.

    The confirmation screen carries this back, and the write refuses if it
    no longer matches -- a compare-and-set, the same shape as every other
    toggle in this codebase. Between the screen and the submit, another
    approval can land or the booking can change; without this the human
    would be answering a question about values that are no longer the ones
    being destroyed.
    """
    digest = hashlib.sha256()
    for loss in found:
        digest.update(f"{loss.field}\x00{loss.current}\x00{loss.incoming}\x00".encode("utf-8"))
    return digest.hexdigest()[:32]


def apply_choices(fresh: dict, document: Document, keep_fields: set[str]) -> dict:
    """Fresh content with the kept fields taken from the CURRENT document.

    Values come from the document as it stands at write time, never from
    the form: a value that travelled through a browser and back is a value
    that could have gone stale or been tampered with, and this is the
    contract path.
    """
    content = dict(fresh)
    current_content = document.content or {}
    for name in keep_fields:
        if name in PROTECTED_TEXT_FIELDS:
            content[name] = current_content.get(name)
    return content


def summarise(found: list[ContentLoss], keep_fields: set[str]) -> str:
    """One audit line: what a human chose to keep and what they let go."""
    kept = sorted(loss.label for loss in found if loss.field in keep_fields)
    replaced = sorted(loss.label for loss in found if loss.field not in keep_fields)
    parts = []
    if kept:
        parts.append("kept " + ", ".join(kept))
    if replaced:
        parts.append("replaced " + ", ".join(replaced))
    return "; ".join(parts) or "no human values affected"
