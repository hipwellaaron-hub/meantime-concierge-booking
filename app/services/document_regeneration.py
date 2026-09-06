"""What a regenerate is about to destroy, named field by field.

Regenerating a document builds fresh content from the booking and replaces
the current version with it. For everything the booking computes -- the
timeline, the food order, totals, the room -- that is exactly right and is
the whole reason the button exists.

For the fields a person writes in their own words it is not. Those values
are not derivable from anything: an approved allergy note, a hand-edited
contract clause. Regenerating discarded them silently, and silence is the
worst property that failure could have. Proved on 2026-09-06, twice:

  - generate_beo_content defaults Dietaries to "No dietary requirements
    declared", so one click of Regenerate replaced a declared nut allergy
    with that sentence and made it version 2 -- Aaron's original incident,
    in a new costume, with no human in the loop at all;
  - regenerating an AGREEMENT discarded a hand-edited special condition
    ("Client may bring their own celebrant. Agreed by Aaron.") the same
    way. That one is the contract.

So a regenerate that would destroy a human value now stops and says
exactly what it is about to discard, and the human decides per field.
Nothing here decides for them; it only refuses to decide silently.

Scope, deliberately: the fields below and no others. Diffing the derived
structures too would produce a screen nobody reads, and a screen nobody
reads is the silence this module exists to end.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BookingEvent, Document
from app.models.beo_proposal import FIELD_APPROVED, BeoProposal, BeoProposalField
from app.services import beo_rules
from app.services.document_generation import DERIVED_KEYS, mark_authored, mark_derived

logger = logging.getLogger(__name__)


def _render_text(value: object) -> str:
    """One spelling for comparison. A CRLF/LF difference is not an edit --
    the same lesson as the approval box (2026-09-06 review)."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _render_sections(value: object) -> str:
    """An agreement's terms are a list of {heading, body}, not a string.
    Rendered to text so the same comparison and the same screen work for
    them -- without this they compared as "" and the contract's own clauses
    were the one thing the guard could not see (2026-09-06 review)."""
    if not isinstance(value, list):
        return ""
    blocks = []
    for section in value:
        if not isinstance(section, dict):
            continue
        heading = _render_text(section.get("heading"))
        body = _render_text(section.get("body"))
        if heading or body:
            blocks.append(f"{heading}\n{body}".strip())
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class ProtectedField:
    name: str
    label: str
    render: Callable[[object], str] = _render_text
    # Fields derived from this one, which must travel with it. An
    # agreement's terms_text is rebuilt from terms_sections, so keeping the
    # sections while letting the text regenerate would leave the document
    # stating two different sets of terms.
    companions: tuple[str, ...] = ()


_TEXT_FIELD_LABELS = {
    **beo_rules.FIELD_LABELS,
    "music_entertainment": "Music & entertainment",
    "internal_notes": "Internal notes (staff/kitchen)",
    "status_text": "Status text",
}

# The Event Order's ten proposable fields, the two free-text fields that
# predate proposals, the legacy merged music field -- and the agreement's
# terms, which are the contract itself.
PROTECTED_FIELDS: tuple[ProtectedField, ...] = tuple(
    ProtectedField(
        name,
        _TEXT_FIELD_LABELS.get(name, name.replace("_", " ").capitalize()),
        # The older merged field is printed only while there is no split
        # `music` (document.html renders `music or music_entertainment`), so
        # keeping one without the other stores a value nothing will show
        # while the audit line says it was kept.
        companions=("music",) if name == "music_entertainment" else (),
    )
    for name in beo_rules.PROPOSABLE_FIELDS + ("music_entertainment", "internal_notes", "status_text")
) + (
    ProtectedField("terms_sections", "Agreement terms", render=_render_sections, companions=("terms_text",)),
)

PROTECTED_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in PROTECTED_FIELDS)
_BY_NAME = {f.name: f for f in PROTECTED_FIELDS}


def field_spec(name: str) -> ProtectedField | None:
    """The protected field of this name, or None if it is not one."""
    return _BY_NAME.get(name)

# Which keys a person authored is READ, not guessed. document_generation
# stamps the keys it derived; documents.update_content and
# update_content_fields take a key out of that set the moment a human
# writes it. What is left is exactly what may be rebuilt without asking.
#
# The set this replaced held fixed placeholder strings and asked "does the
# current text look generated?" -- which is unanswerable as soon as the
# generator composes a value instead of emitting a constant, and it does
# that for the agreement's clauses, the bar-credit line, status text and
# the guest counts in special notes. Every one of those came back "a person
# wrote this", pre-ticked to keep, so one click on the safe-looking default
# wrote a stale figure onto the contract (2026-09-07 review).

# Writes that mean a human authored this version of the document. Recorded
# per version, not per field, which is why a legacy document can only ever
# say "someone edited this" and not which key.
_HUMAN_WRITE_EVENTS = ("document_edited", "beo_proposal_applied")


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
        """The worst shape: real words replaced by nothing at all."""
        return not self.incoming


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
        if _render_text(row.applied_value) == current:
            # No %-d: it is a glibc extension and raises on Windows, where
            # the tests run.
            when = row.decided_at.strftime("%d %b %Y").lstrip("0") if row.decided_at else "an earlier date"
            who = row.decided_by or "staff"
            return f"approved by {who} on {when}"
    return None


def _human_writes(db: Session, document: Document) -> list[BookingEvent]:
    """Every write to THIS version that came from a person -- a hand-edit or
    an approval applying a proposal. Recorded per version, not per field."""
    return list(
        db.scalars(
            select(BookingEvent)
            .where(
                BookingEvent.booking_id == document.booking_id,
                BookingEvent.event_type.in_(_HUMAN_WRITE_EVENTS),
                BookingEvent.field_name == f"{document.type.value}_version",
                BookingEvent.new_value == str(document.version),
            )
            .order_by(BookingEvent.created_at.desc())
        ).all()
    )


def was_hand_edited(db: Session, document: Document) -> BookingEvent | None:
    """The most recent HAND-edit of this version, for the screen to name.
    An approval is not one: it has its own event type, so the confirmation
    no longer tells Aaron a draft was hand-edited by the person who
    approved a proposal on it (2026-09-06 review)."""
    for event in _human_writes(db, document):
        if event.event_type == "document_edited":
            return event
    return None


def authored_keys(db: Session, document: Document) -> set[str] | None:
    """The keys a person wrote, or None when that cannot be known.

    None means a legacy document -- one written before provenance was
    recorded -- that a human has since touched. Its content says nothing
    about which key, so every differing field is treated as at risk and the
    screen says the provenance is unknown rather than implying it is exact.
    """
    content = document.content or {}
    derived = content.get(DERIVED_KEYS)
    if derived is not None:
        return {k for k in content if not k.startswith("_") and k not in set(derived)}
    if not _human_writes(db, document):
        # No provenance recorded, but nothing human was ever written to this
        # version either: the generator produced all of it.
        return set()
    return None


def losses(db: Session, document: Document | None, fresh: dict) -> list[ContentLoss]:
    """The human-written values `fresh` would destroy, in field order.

    Empty when there is nothing to lose -- which is now the ordinary case
    for an untouched document, because the generator's own output is known
    to be its own and is rebuilt without asking.

    This is a READ. A caller that intends to write must hold the document's
    row lock across both, or another approval can land in between and be
    reverted (proved live, 2026-09-06) -- see
    documents.lock_current_for_update.
    """
    if document is None:
        return []
    current_content = document.content or {}
    authored = authored_keys(db, document)
    approved = _approved_values(db, document.booking_id)

    found: list[ContentLoss] = []
    for spec in PROTECTED_FIELDS:
        current = spec.render(current_content.get(spec.name))
        incoming = spec.render(fresh.get(spec.name))
        if current == incoming or not current:
            continue
        if authored is not None and spec.name not in authored:
            continue  # the generator wrote it; rebuilding is the whole point
        found.append(
            ContentLoss(
                field=spec.name,
                label=spec.label,
                current=current,
                incoming=incoming,
                approved_note=_approval_note(approved.get(spec.name, []), current),
            )
        )
    return found


def fingerprint(found: list[ContentLoss], pending: list[dict] | None = None) -> str:
    """Identifies the exact set of losses a human was shown.

    The confirmation screen carries this back, and the write refuses if it
    no longer matches -- a compare-and-set, the same shape as every other
    toggle in this codebase. It guards against answering a question about
    values that have since changed; it is NOT a substitute for the row
    lock, because on its own it leaves a window between check and write.
    """
    digest = hashlib.sha256()
    for loss in found:
        digest.update(f"{loss.field}\x00{loss.current}\x00{loss.incoming}\x00".encode("utf-8"))
    # The pending proposals were on the same screen, so they are part of
    # what the human was answering about: a proposal arriving in between
    # has to re-ask, not be silently invalidated by the write.
    for row in pending or []:
        digest.update(f"pending\x00{row.get('id')}\x00{row.get('field')}\x00".encode("utf-8"))
    return digest.hexdigest()[:32]


def apply_choices(fresh: dict, document: Document, keep_fields: set[str]) -> dict:
    """Fresh content with the kept fields taken from the CURRENT document.

    Values come from the document as it stands at write time, never from
    the form: a value that travelled through a browser and back is a value
    that could have gone stale or been tampered with, and this is the
    contract path.

    The authorship travels with the value. A field kept because a person
    wrote it is still theirs in the new version; a field allowed to
    regenerate is the generator's again. Without that, one regenerate would
    launder a human value into a derived one and the NEXT regenerate would
    discard it without asking.
    """
    content = dict(fresh)
    current_content = document.content or {}
    kept: list[str] = []
    for name in keep_fields:
        spec = _BY_NAME.get(name)
        if spec is None:
            continue
        content[spec.name] = current_content.get(spec.name)
        kept.append(spec.name)
        for companion in spec.companions:
            # Derived from the kept value; letting it regenerate would
            # leave the document asserting two different things.
            content[companion] = current_content.get(companion)
            kept.append(companion)
    mark_derived(content, [f.name for f in PROTECTED_FIELDS if f.name not in kept])
    return mark_authored(content, kept)


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
