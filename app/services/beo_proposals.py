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
import json
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select, update, text
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, MenuItem
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.utils import truncate
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
from app.models.wizard_session import WizardSessionStatus
from app.services.document_generation import (
    NO_DIETARIES,
    REVIEW,
    build_total_food_spend,
    compute_food_order_total,
    generate_beo_content,
)
from app.services import beo_rules, catalogue, document_regeneration, documents as documents_service
from app.models.booking import BLOCKING_STATUSES
from app.models.booking import BLOCKING_STATUSES
from app.models.booking import BLOCKING_STATUSES
from app.models.booking import BLOCKING_STATUSES
from app.services.booking import VOIDED_STATUSES
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# The document never prints a blank Dietaries section: an empty value
# reads as this sentence, so "nothing declared" is a statement rather
# than an oversight. One definition, in the generator that writes it
# first; the hand-edit form, the regenerate placeholder check and an
# approved proposal all import it (see NO_DIETARIES below).

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


def fresh_beo_content(db: Session, booking: Booking) -> dict:
    """What Generate builds for this booking right now: the wizard's own
    answers when the client has submitted it, else the booking's facts
    with [REVIEW] prompts. One definition for the staff click and for a
    proposal creating the first draft, so the two cannot drift."""
    session = booking.wizard_session
    if session is not None and session.status == WizardSessionStatus.submitted:
        # A completed wizard already has the client's real food/beverage/
        # music/extras answers -- generating blind placeholders instead
        # would throw that away (see app.services.wizard_generation).
        from app.services import wizard_generation

        return wizard_generation.build_beo_content_for_session(db, session)
    return generate_beo_content(booking)


def _create_draft_for_proposal(db: Session, booking: Booking, *, actor: str, content: dict) -> tuple[Document | None, bool]:
    """(the draft to attach the proposal to, whether THIS call created it).

    Aaron, 2026-09-11: "The proposal should be able to create the Event
    Order draft if none exists, rather than refusing until I've generated
    one by hand." Called only once the rules have passed against
    `content`, the draft's own would-be values -- a blocked proposal
    creates nothing. Only a tentative/confirmed/completed booking (the
    caller checks): an enquiry's Event Order is a staff decision.

    No commit here. The draft, its trail row and the proposal land in the
    caller's one transaction, under the advisory lock the whole propose
    holds, so neither can exist without the other and a proposal waiting
    behind this one cannot slip in between (review, 2026-09-11).

    The booking-row lock is the one thing that closes the window against a
    staff Generate at the same instant: with no Event Order there is no
    document row for lock_current_for_update to lock, and the Generate
    paths take this same lock before their own locked read. If a Generate
    still got there first, its draft is returned and nothing is created."""
    documents_service.lock_booking_row(db, booking.id)
    current = documents_service.lock_current_for_update(db, booking.id, DocumentType.beo)
    if current is not None:
        if current.status == DocumentStatus.draft and not current.is_legacy:
            return current, False
        return None, False
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor=actor, commit=False
    )
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="beo_draft_by_proposal",
            field_name="beo_version",
            new_value=str(document.version),
            actor=actor,
        )
    )
    return document, True


# --- the food order: catalogue items and quantities, never a price -----------

FOOD_ORDER_FIELD = beo_rules.FOOD_ORDER_FIELD
# Cakes are their own catalogue category (the wizard's cake picker reads
# it) but print under Desserts on the Event Order, exactly as the wizard's
# own line builder maps them.
_CATEGORY_ON_DOCUMENT = {"cake": "dessert"}
_PRICE_KEYS = ("unit_price", "price", "amount", "total", "line_total", "cost")


def _fold_name(name: str) -> str:
    return " ".join(str(name).lower().replace("&", "and").split())


def _selection_json(lines: list[dict]) -> str:
    """The canonical spelling of a food selection: catalogue ids and
    quantities, IN THE ORDER PROPOSED -- which is the order the client
    asked for them in, the order the panel renders and the order the form
    posts back, so an unedited approval compares equal without sorting
    anything and the Event Order's lines do not arrive in UUID order.
    NO NAMES -- `edited_before_approval` compares this
    string against the approved one, and a catalogue rename between
    propose and approve otherwise reported a staff edit that never
    happened, and wrote a beo_proposal_edited event whose old and new
    halves were identical (review, 2026-09-11). Names are read from the
    catalogue wherever one is printed."""
    return json.dumps(
        [{"menu_item_id": str(ln["menu_item_id"]), "quantity": int(ln["quantity"])} for ln in lines],
        separators=(",", ":"),
    )


def selection_text(db: Session, selection: list) -> str:
    """"2 x Grazing Platter; 3 x Pork Belly Bites" -- what was PROPOSED,
    priced by nobody. Read with get_by_id_any so a retired item still
    prints its name: this is the "before" half of the edited event, and a
    line dropped at approval has to be visible in it."""
    parts = []
    for entry in selection:
        if not isinstance(entry, dict):
            continue
        item = catalogue.get_by_id_any(db, entry["menu_item_id"]) if entry.get("menu_item_id") else None
        name = item.name if item is not None else (entry.get("name") or "an item no longer in the catalogue")
        parts.append(f"{entry.get('quantity')} x {name}")
    return "; ".join(parts)


def _load_selection(text: str | None) -> list:
    try:
        value = json.loads(text or "[]")
    except ValueError as exc:
        raise ProposalError("the stored food order proposal could not be read") from exc
    return value if isinstance(value, list) else []


def resolve_food_selection(
    db: Session, booking: Booking, selection: object, *, already_ordered: set | None = None
) -> tuple[list[dict], list]:
    """[{menu_item_id | name, quantity}, ...] -> priced lines, and every
    reason a line could not be priced, as RuleViolations.

    Only ACTIVE catalogue items, by id (from `catalogue`) or by exact name
    (case and spacing forgiven, '&' read as 'and') -- EXCEPT an id in
    `already_ordered`, which is on this booking's Event Order already.
    Retirement means "no longer offered", never "your existing order is
    now unpriceable" (catalogue.get_by_id_any says so, and the wizard's
    line builder reads it that way). Without this, an order carrying a
    since-retired line could not be re-proposed at all, and any proposal
    that omitted it silently dropped it, because an approval REPLACES the
    food order (review, 2026-09-11). The price is the
    catalogue's for THIS booking (catalogue.resolve_price: legacy pizza
    pricing honoured; None is a refusal, never a guess). A line that
    carries any price key is refused outright: the AI never writes a
    price. Quantities are whole numbers 1..500; one line per item.

    Resolved lines: {menu_item_id, name, category (as printed on the
    Event Order), quantity, unit_price (str)}."""
    V = beo_rules.RuleViolation
    F = FOOD_ORDER_FIELD
    violations: list = []
    if not isinstance(selection, list) or not selection:
        violations.append(V(beo_rules.FOOD_EMPTY, F, "A food order proposal has to name at least one catalogue item."))
        return [], violations
    if len(selection) > beo_rules.MAX_FOOD_LINES:
        violations.append(V(beo_rules.FOOD_EMPTY, F, f"A food order proposal has at most {beo_rules.MAX_FOOD_LINES} lines."))
        return [], violations
    active = list(db.scalars(select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.name)).all())
    by_id = {str(item.id): item for item in active}
    for ordered_id in already_ordered or ():
        if str(ordered_id) not in by_id:
            retired = catalogue.get_by_id_any(db, ordered_id)
            if retired is not None:
                by_id[str(retired.id)] = retired
    by_name: dict[str, list[MenuItem]] = {}
    for item in active:
        by_name.setdefault(_fold_name(item.name), []).append(item)
    listing = "; ".join(f"{item.name} ({item.category.value})" for item in active)

    seen: set[str] = set()
    lines: list[dict] = []
    for raw in selection:
        if not isinstance(raw, dict):
            violations.append(V(beo_rules.FOOD_UNKNOWN_ITEM, F, "Each food line must be an object with menu_item_id or name, and quantity."))
            continue
        priced = [k for k in _PRICE_KEYS if k in raw]
        if priced:
            violations.append(
                V(
                    beo_rules.FOOD_PRICE_SENT, F,
                    f"A food line carried a price ({', '.join(priced)}). The AI never writes a price: send the "
                    "catalogue item and the quantity, and the price comes from the catalogue.",
                )
            )
            continue
        item = None
        if raw.get("menu_item_id"):
            item = by_id.get(str(raw["menu_item_id"]))
            if item is None:
                violations.append(
                    V(beo_rules.FOOD_UNKNOWN_ITEM, F, f"menu_item_id {str(raw['menu_item_id'])[:60]!r} is not an active catalogue item. Read `catalogue` for the ids. Active items: {listing}.")
                )
                continue
        elif raw.get("name"):
            matches = by_name.get(_fold_name(raw["name"]), [])
            if len(matches) == 1:
                item = matches[0]
            elif matches:
                violations.append(
                    V(beo_rules.FOOD_AMBIGUOUS_ITEM, F, f"{str(raw['name'])[:80]!r} matches more than one catalogue item ({', '.join(m.category.value for m in matches)}); send its menu_item_id instead.")
                )
                continue
            else:
                violations.append(
                    V(beo_rules.FOOD_UNKNOWN_ITEM, F, f"{str(raw['name'])[:80]!r} is not an active catalogue item by that name. Active items: {listing}.")
                )
                continue
        else:
            violations.append(V(beo_rules.FOOD_UNKNOWN_ITEM, F, "A food line needs a menu_item_id or a name."))
            continue
        quantity = raw.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity > beo_rules.MAX_FOOD_LINE_QUANTITY:
            violations.append(
                V(beo_rules.FOOD_BAD_QUANTITY, F, f"{item.name}: quantity must be a whole number from 1 to {beo_rules.MAX_FOOD_LINE_QUANTITY} (got {quantity!r}).")
            )
            continue
        if str(item.id) in seen:
            violations.append(V(beo_rules.FOOD_DUPLICATE_ITEM, F, f"{item.name} appears twice; send one line per item with the total quantity."))
            continue
        seen.add(str(item.id))
        price = catalogue.resolve_price(item, booking)
        if price is None:
            violations.append(
                V(
                    beo_rules.FOOD_PRICE_UNAVAILABLE, F,
                    f"{item.name} has no price on record for this booking (legacy-priced booking, no legacy "
                    "price was ever defined), so it cannot be proposed -- staff price it by hand.",
                )
            )
            continue
        lines.append(
            {
                "menu_item_id": str(item.id),
                "name": item.name,
                "category": _CATEGORY_ON_DOCUMENT.get(item.category.value, item.category.value),
                "quantity": quantity,
                "unit_price": str(price),
            }
        )
    return lines, violations


def ordered_item_ids(document: Document | None) -> set:
    """The catalogue ids already on this Event Order's food order."""
    return {str(ln["menu_item_id"]) for ln in current_food_lines(document) if ln.get("menu_item_id")}


def food_total(lines: list[dict]) -> Decimal:
    return sum((Decimal(str(ln["quantity"])) * Decimal(str(ln["unit_price"])) for ln in lines), Decimal("0.00"))


def food_text(lines: list[dict]) -> str:
    """One readable line for the trail and the review row: what the money
    is, the way _render_food_order says it."""
    if not lines:
        return ""
    parts = [f"{ln['quantity']} x {ln.get('name') or ln.get('description')} @ {ln['unit_price']}" for ln in lines]
    return "; ".join(parts) + f" = {food_total(lines):.2f}"


def current_food_lines(document: Document | None) -> list[dict]:
    """The Event Order's stored food lines, in the review row's shape."""
    content = (document.content if document is not None else None) or {}
    order = content.get("food_order") if isinstance(content.get("food_order"), dict) else {}
    out = []
    for raw in order.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(
                {
                    "menu_item_id": raw.get("menu_item_id"),
                    "name": raw.get("description") or raw.get("item") or "",
                    "category": raw.get("category") or "",
                    "quantity": int(raw.get("quantity") or 0),
                    "unit_price": str(Decimal(str(raw.get("unit_price") or "0")).quantize(Decimal("0.01"))),
                }
            )
        except (ValueError, ArithmeticError):
            continue
    return out


def _is_catalogue_built(invoice) -> bool:
    """Whether every charge line on this invoice came from this sync.

    A catalogue line carries its menu_item_id; nothing else writes one.
    So this answers "is this invoice entirely mine to rebuild?" -- and
    only then is replacing its lines safe. The staff invoice form, and
    the wizard's own line builder, both write lines WITHOUT an id, which
    is why the id is the right question and "is it food?" is not: the
    wizard's platters and its priced in-house cake would pass a food
    test and be double-billed by a merge (review, 2026-09-11)."""
    from app.services import invoicing

    charges = [
        line
        for line in (invoice.line_items or [])
        if isinstance(line, dict) and line.get("description") != invoicing.DEPOSIT_CREDIT_DESCRIPTION
    ]
    # An EMPTY draft (no charge lines at all) counts as mine: there is
    # nothing on it to lose, and refusing to fill it would raise a banner
    # asking somebody to reconcile an invoice with nothing on it.
    return all(line.get("menu_item_id") for line in charges)


def final_invoice_prefill(document) -> list[dict]:
    """The Event Order's food order as invoice line rows, for the booking
    page's Create-final-invoice form to arrive already filled in.

    Returns [] when there is no current Event Order or it holds no food --
    the form then looks exactly as it did before.

    A PREFILL, deliberately, not an auto-create. sync_final_invoice_from_food
    above does build the invoice outright, but only off an approved
    PROPOSAL; a booking whose food was typed straight into the Event Order
    never reaches it, and every migrated booking is in that shape. Making
    that function fire on any Event Order would start writing invoices
    nobody asked for, so this fills the form and a person still decides.

    The name is read across all three shapes this key has had --
    "description", "item", "name" -- because the bookings that need this
    most are the migrated ones carrying the older keys. Same order as
    _normalise_food_lines, which this has to agree with.
    """
    if document is None:
        return []
    content = document.content or {}
    lines = (content.get(FOOD_ORDER_FIELD) or {}).get("line_items") or []
    rows = []
    for raw in lines:
        if not isinstance(raw, dict):
            continue
        name = raw.get("description") or raw.get("item") or raw.get("name") or ""
        if not name:
            continue
        rows.append({
            "description": name,
            "quantity": raw.get("quantity") or 1,
            "unit_price": raw.get("unit_price") or "",
        })
    return rows


def sync_final_invoice_from_food(db: Session, booking: Booking, lines: list[dict], *, actor: str) -> str:
    """The second half of the ruling: "the line items and invoice are
    built from the catalogue." Runs after the approved lines are on the
    Event Order.

    With no final invoice it CREATES the draft from the same priced lines
    (the deposit credit is applied by create_final_invoice). A DRAFT THIS
    SYNC BUILT is refreshed. Anything else -- a draft carrying a line a
    person or the wizard put there, an invoice that has gone out or been
    paid, a legacy record -- is LEFT EXACTLY ALONE, because rebuilding it
    would delete work this path never wrote: room hire, a bar tab, a
    negotiated discount, the wizard's own in-house cake line. The reason
    goes on the trail and onto the booking page for a person to settle.

    Its own transaction, after the document's: a failure here leaves the
    approved lines on the Event Order and the reason on the trail, and
    the booking page's own final-invoice form still works."""
    from app.services import invoicing, policy

    invoice_lines = [
        {
            "description": ln["name"],
            "quantity": ln["quantity"],
            "unit_price": ln["unit_price"],
            "category": ln["category"],
            "menu_item_id": ln["menu_item_id"],
        }
        for ln in lines
    ]
    if booking.status in VOIDED_STATUSES:
        # Approving a run sheet on a cancelled booking is a staff
        # decision; billing for it is not something to do automatically.
        outcome = f"no final invoice built: this booking is {booking.status.value}"
        existing = None
    else:
        existing = db.execute(
            select(Invoice).where(
                Invoice.booking_id == booking.id,
                Invoice.type == InvoiceType.final,
                Invoice.status != InvoiceStatus.cancelled,
            ).order_by(Invoice.created_at.desc())
        ).scalars().first()
        outcome = None
    try:
        if outcome is not None:
            pass
        elif existing is None:
            due = policy.final_balance_due_date(booking.event_date, issued_on=dt.date.today())
            if due is None:
                outcome = "no final invoice built: the booking has no event date for it to fall due against"
            else:
                invoice = invoicing.create_final_invoice(db, booking, line_items=invoice_lines, due_date=due, actor=actor)
                outcome = f"draft final invoice #{invoice.invoice_number} built from the approved food order (total {invoice.total})"
        elif existing.is_legacy:
            outcome = f"final invoice #{existing.invoice_number} is a legacy record and was left alone"
        elif existing.status != InvoiceStatus.draft:
            outcome = (
                f"final invoice #{existing.invoice_number} is already {existing.status.value} and was left alone -- "
                + (
                    "cancel and reissue it if the food order changed"
                    if existing.status == InvoiceStatus.paid
                    else "revise it by hand if the food order changed"
                )
            )
        elif not _is_catalogue_built(existing):
            outcome = (
                f"draft final invoice #{existing.invoice_number} carries lines this Event Order did not put there, "
                "so it was left alone -- check its food lines against the approved order by hand"
            )
        else:
            invoicing.update_invoice(db, existing, line_items=invoice_lines, due_date=existing.due_date, actor=actor)
            outcome = f"draft final invoice #{existing.invoice_number} refreshed from the approved food order (total {existing.total})"
    except Exception as exc:  # noqa: BLE001 -- an invoice problem must never undo an approved food order
        db.rollback()
        logger.exception("Final invoice sync failed for booking %s", booking.id)
        outcome = f"final invoice not updated: {exc}"
    try:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="final_invoice_from_beo",
                field_name=FOOD_ORDER_FIELD,
                new_value=truncate(outcome, 500),
                actor=actor,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 -- the trail is the last thing; it cannot undo the rest
        db.rollback()
        logger.exception("Could not record the final-invoice outcome for booking %s", booking.id)
    return outcome


def latest_food_invoice_notice(booking: Booking) -> str | None:
    """What the booking page says about the last invoice sync -- only when
    it did NOT build or refresh a draft, which is the case a person has to
    act on. A built or refreshed draft is visible in the invoice list.

    RETIRES ITSELF once somebody has acted: any invoice event after the
    one that raised it (a revise, a cancel, an edit, a send) means the
    question has been looked at, and a banner that never clears is one
    staff learn to scroll past -- which is the failure it exists to
    prevent, not a smaller version of it (review, 2026-09-11)."""
    raised = None
    acted_after = False
    for event in booking.events:  # ordered by created_at
        if event.event_type == "final_invoice_from_beo":
            raised = event
            acted_after = False
        elif raised is not None and event.event_type in (
            "invoice_created", "invoice_edited", "invoice_status_changed", "invoice_deleted", "payment_received"
        ):
            acted_after = True
    if raised is None or acted_after:
        return None
    if (raised.new_value or "").startswith("draft final invoice #") and "left alone" not in (raised.new_value or ""):
        return None
    return raised.new_value


def _deposit_paid_for(db: Session, document: Document) -> Decimal:
    """The deposit the Event Order's total block reports: what has
    actually been paid, the same question invoicing.get_deposit_paid
    answers for the wizard's own total.

    0.00 is a FACT, not an unknown. An earlier version of this returned
    None when nothing had been paid, which made build_total_food_spend
    print "[REVIEW] deposit paid / balance due aren't derivable yet --
    payments aren't tracked in Concierge until Phase 3" on an approval --
    a sentence that stopped being true when payments started being
    tracked, over a figure this function had just read (review,
    2026-09-11).

    The hand-edit form still keeps whatever the document already printed
    rather than re-reading payments; that divergence predates this and is
    recorded in docs/event-order-proposals.md."""
    from app.services import invoicing

    return invoicing.get_deposit_paid(db, document.booking)


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
    return current_values_from_content(document.content if document is not None else None)


def current_values_from_content(content: dict | None) -> dict[str, str]:
    """current_values for content that may not be a document yet: the
    draft a proposal is about to create is judged BEFORE it exists."""
    content = content or {}
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


def latest_proposal(db: Session, booking_id: uuid.UUID) -> BeoProposal | None:
    """The newest proposal for this booking, whatever became of it.

    pending_proposal answers "is anything outstanding". This answers "what
    happened to the last ask", which is a different question and the one
    the calibration read needs: applied_value and edited_before_approval
    only exist once a human has DECIDED a field, and deciding the last one
    resolves the proposal -- so a reader restricted to pending rows goes
    blank at the exact moment the answer appears.

    One proposal, not a history. A newer ask -- including one the house
    rules blocked -- hides the decided one behind it, which is right for
    the read immediately before the next propose and is why this is not a
    record of every correction ever made.

    created_at alone does not order these. It is `now()`, which in Postgres
    is the TRANSACTION's start time, so two proposals created in one
    transaction carry the same timestamp to the microsecond -- every test
    that makes two, and any future caller that batches. Superseding is what
    tells those apart: _supersede_older marks the older pending proposals
    when a new one lands, so a superseded row always has something newer
    behind it and belongs last. Two rows that tie with neither superseding
    the other (a blocked ask does not supersede) are ordered by id: stable,
    so nothing flickers between runs, and arbitrary, which is the honest
    answer when nothing in the data says which came first.
    """
    return db.scalars(
        select(BeoProposal)
        .where(BeoProposal.booking_id == booking_id)
        .order_by(
            BeoProposal.created_at.desc(),
            (BeoProposal.status == STATUS_SUPERSEDED).asc(),
            BeoProposal.id.desc(),
        )
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
    the timeline can explain where a proposed value went.

    Both writes are compare-and-set in SQL, not read-then-set in Python.
    The advisory lock serialises proposes with each other only; an
    approval runs under a row lock this function never took, so a Python
    check of `state == pending` could read pending, block on the
    approval's row lock, and then overwrite the just-committed "approved"
    the instant it released -- leaving a field that was applied to the
    document recorded as superseded, invisible to every count of
    approvals, which is the one measure this feature exists to produce
    (ultrareview, 2026-09-06). `WHERE state = 'pending'` is re-evaluated
    by Postgres after the lock is granted, so an approved row is skipped.
    """
    now = dt.datetime.now(dt.timezone.utc)
    older = db.scalars(
        select(BeoProposal).where(
            BeoProposal.booking_id == booking_id, BeoProposal.status == STATUS_PENDING
        )
    ).all()
    for proposal in older:
        dropped = db.execute(
            update(BeoProposalField)
            .where(BeoProposalField.proposal_id == proposal.id, BeoProposalField.state == FIELD_PENDING)
            .values(state=FIELD_SUPERSEDED)
            .returning(BeoProposalField.field, BeoProposalField.proposed_value)
            .execution_options(synchronize_session=False)
        ).all()
        for field_name, proposed_value in dropped:
            db.add(
                BookingEvent(
                    booking_id=booking_id,
                    event_type="beo_proposal_superseded",
                    field_name=field_name,
                    old_value=proposed_value or None,
                    actor=actor,
                )
            )
        # The parent row the same way: an approval that resolved this
        # proposal in the meantime must keep "resolved", not be relabelled.
        db.execute(
            update(BeoProposal)
            .where(BeoProposal.id == proposal.id, BeoProposal.status == STATUS_PENDING)
            .values(status=STATUS_SUPERSEDED, resolved_at=now)
            .execution_options(synchronize_session=False)
        )
        # The ORM copies were read before the SQL ran; drop them so nothing
        # later flushes a stale state over what the database now holds.
        for field_row in proposal.fields:
            db.expire(field_row)
        db.expire(proposal)


def _printed_legacy_music(content: dict) -> str | None:
    """The older merged music/entertainment value, but only when it is
    what the Event Order actually prints: the template shows `music` if
    there is one and falls back to the merged field otherwise. A merged
    value behind a split `music` is dead weight, and the generator's own
    "[REVIEW] add music/entertainment detail" prompt is not a value a
    person wrote -- treating either as legacy text refused every proposal
    that touched Music on a freshly generated Event Order."""
    if content.get("music"):
        return None
    legacy = content.get("music_entertainment")
    if not isinstance(legacy, str) or not legacy.strip() or legacy.lstrip().startswith(REVIEW):
        return None
    return legacy


def _rule_context(booking: Booking, document: Document | None = None) -> dict:
    return _rule_context_from_content(booking, document.content if document is not None else None)


def _rule_context_from_content(booking: Booking, content: dict | None) -> dict:
    content = content or {}
    return {
        # The older merged field, so LEGACY_MUSIC_SPLIT can refuse a Music
        # write that would drop the entertainment half of it.
        "legacy_music_entertainment": _printed_legacy_music(content),
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
    food_order: list | None = None,
) -> tuple[BeoProposal, beo_rules.RuleResult]:
    """Store one proposal. Always writes a row -- a blocked proposal is
    kept for calibration, exactly as a rules-blocked draft is -- and
    returns it with the rule result so the caller can answer the AI.

    On a tentative/confirmed/completed booking with no Event Order, a
    proposal that passes the rules also CREATES the first draft, in the
    same transaction (Aaron, 2026-09-11); `proposal.draft_created` says
    whether this call did.

    `food_order` is [{menu_item_id | name, quantity}, ...]: catalogue
    items and quantities, resolved and priced here from the catalogue
    (resolve_food_selection); a line that cannot be priced blocks the
    proposal like any other rule. Never applies anything.
    """
    refusal = can_receive_proposals(booking)
    if refusal is not None:
        raise ProposalError(refusal)

    _proposal_lock(db, booking.id)
    document = current_draft_beo(db, booking.id)
    proposed = {name: normalise_newlines(value).strip() for name, value in (fields or {}).items()}

    # What the rules are judged against, and whether this proposal will
    # create the draft. Three states besides "a draft exists":
    #   - a current Event Order that has gone out (sent/viewed/signed):
    #     judged against ITS values -- what a Revise copies forward and
    #     what approval will see -- and left alone; the proposal waits for
    #     a staff Revise (a legacy record is a placeholder: judged blank);
    #   - no Event Order on a tentative/confirmed/completed booking: judged
    #     against the content the draft WOULD hold, so a blocked proposal
    #     creates nothing and what was checked is what gets created;
    #   - no Event Order on an enquiry: judged blank, no draft -- an
    #     enquiry's Event Order is a staff decision (parity with a client
    #     who has not yet signed or paid).
    judged_content: dict | None = None
    will_create = False
    if document is not None:
        judged_content = document.content
    else:
        current_any = documents_service.get_current(db, booking.id, DocumentType.beo)
        if current_any is not None:
            judged_content = None if current_any.is_legacy else current_any.content
        elif booking.status in BLOCKING_STATUSES:
            judged_content = fresh_beo_content(db, booking)
            will_create = True
    current = current_values_from_content(judged_content)

    if proposed or food_order is None:
        result = beo_rules.validate(proposed, current=current, **_rule_context_from_content(booking, judged_content))
    else:
        # A food-only proposal: no text fields for the text rules to judge.
        result = beo_rules.RuleResult()
    food_lines: list[dict] = []
    if food_order is not None:
        # Priced here from the catalogue for THIS booking; a line that
        # cannot be priced blocks the proposal like any text rule.
        food_lines, food_violations = resolve_food_selection(
            db, booking, food_order, already_ordered=ordered_item_ids(document)
        )
        result.violations.extend(food_violations)

    created_draft = False
    if not result.blocked and will_create:
        document, created_draft = _create_draft_for_proposal(db, booking, actor=actor, content=judged_content)

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
    if food_order is not None:
        # The selection as resolved (ids and names), or -- when it could
        # not be resolved, so the proposal is blocked -- as sent, for the
        # calibration record.
        db.add(
            BeoProposalField(
                proposal_id=proposal.id,
                field=FOOD_ORDER_FIELD,
                state=FIELD_BLOCKED if result.blocked else FIELD_PENDING,
                proposed_value=_selection_json(food_lines) if food_lines and not result.blocked
                else json.dumps(food_order, default=str)[:20000],
            )
        )
        proposed = {**proposed, FOOD_ORDER_FIELD: ""}  # so the event names it

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
    try:
        db.commit()
    except IntegrityError as exc:
        # The partial unique index on the current version, or the one on
        # "one pending proposal per booking": another writer landed in the
        # same instant despite the locks. Nothing is half-written; the AI
        # is told to propose again rather than shown a 500.
        db.rollback()
        raise ProposalError("another write landed on this booking at the same moment -- propose again") from exc
    db.refresh(proposal)
    # Transient, for the API answer: whether THIS proposal made the draft.
    # Not a column -- the trail row beo_draft_by_proposal is the record.
    proposal.draft_created = created_draft
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


def _check_on_approval(document: Document, changes: dict[str, str], current: dict[str, str]) -> None:
    """The house rules, on the values actually being written. The box is
    editable, so this is the check that cannot be pasted past. Text
    fields only; the food order is checked by _food_lines_for_apply."""
    text_changes = {k: v for k, v in changes.items() if k != FOOD_ORDER_FIELD}
    if not text_changes:
        return
    result = beo_rules.validate(text_changes, current=current, **_rule_context(document.booking, document))
    if result.blocked:
        raise ProposalError(result.as_note())


def _food_lines_for_apply(db: Session, document: Document, proposed_json: str, applied_json: str) -> list[dict]:
    """What approving the food row writes: the selection as it stands NOW
    (quantities may have been edited; a 0 removes the line), resolved and
    priced from the catalogue at the moment of approval -- exactly as the
    wizard prices its lines. Anything unpriceable refuses the approval
    with the reason; nothing is ever guessed.

    A PROPOSED LINE MAY ONLY LEAVE BY AN EXPLICIT 0. The review panel used
    to render only the lines it could price, so approving from it posted a
    shorter selection and this resolved it cleanly: a $300 line vanished
    from a client's Event Order under a banner saying the proposal could
    not be approved as it stood (proved 2026-09-11). Approving a subset is
    now a refusal, whatever posted it."""
    proposed = _load_selection(proposed_json)
    submitted = {
        str(ln["menu_item_id"]): ln
        for ln in _load_selection(applied_json)
        if isinstance(ln, dict) and ln.get("menu_item_id")
    }
    missing = [
        entry for entry in proposed
        if isinstance(entry, dict) and str(entry.get("menu_item_id")) not in submitted
    ]
    if missing:
        raise ProposalError(
            "the approval left out "
            + selection_text(db, missing)
            + " -- reload the Event Order and set a line to 0 to drop it, rather than approving part of a proposal"
        )
    selection = [
        {"menu_item_id": ln.get("menu_item_id"), "name": ln.get("name"), "quantity": ln.get("quantity")}
        for ln in submitted.values()
        if ln.get("quantity") not in (0, "0", None)
    ]
    if not selection:
        raise ProposalError("every food line was set to 0 -- reject the food order rather than approving nothing")
    lines, violations = resolve_food_selection(
        db, document.booking, selection, already_ordered=ordered_item_ids(document)
    )
    if violations:
        raise ProposalError(" ".join(v.message for v in violations))
    return lines


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
    changes: dict[str, object] = {}
    now = dt.datetime.now(dt.timezone.utc)
    approved_food_lines: list[dict] | None = None
    for field_row, applied in decisions:
        if field_row.field == FOOD_ORDER_FIELD:
            # Priced from the catalogue now; the document gets the lines
            # in the shape every reader of food_order already knows, plus
            # the item id so a catalogue line is recognisable later. The
            # total block is rebuilt with it -- lines and heading must
            # never disagree (the 2026-09-08 defect).
            lines = _food_lines_for_apply(db, document, field_row.proposed_value, applied)
            approved_food_lines = lines
            before = current_food_lines(document)
            changes[FOOD_ORDER_FIELD] = {
                "line_items": [
                    {
                        "description": ln["name"],
                        "quantity": ln["quantity"],
                        "unit_price": ln["unit_price"],
                        "category": ln["category"],
                        "menu_item_id": ln["menu_item_id"],
                    }
                    for ln in lines
                ],
                "note": None,
            }
            changes["total_food_spend"] = build_total_food_spend(
                compute_food_order_total(changes[FOOD_ORDER_FIELD]["line_items"]), _deposit_paid_for(db, document)
            )
            field_row.state = FIELD_APPROVED
            field_row.previous_value = _selection_json(before) if before else None
            field_row.applied_value = _selection_json(lines)
            field_row.decided_at = now
            field_row.decided_by = actor
            db.add(
                BookingEvent(
                    booking_id=proposal.booking_id,
                    event_type="beo_proposal_approved",
                    field_name=FOOD_ORDER_FIELD,
                    old_value=food_text(before) or None,
                    new_value=food_text(lines),
                    actor=actor,
                )
            )
            if field_row.edited_before_approval:
                db.add(
                    BookingEvent(
                        booking_id=proposal.booking_id,
                        event_type="beo_proposal_edited",
                        field_name=FOOD_ORDER_FIELD,
                        # From the STORED selection, not a fresh resolve: a
                        # line dropped at approval must appear in the
                        # "before" half, and re-resolving lost exactly that.
                        old_value=selection_text(db, _load_selection(field_row.proposed_value)) or None,
                        new_value=food_text(lines),
                        actor=actor,
                    )
                )
            continue
        changes[field_row.field] = normalise_beo_field(field_row.field, applied)
        if field_row.field == "music":
            # The merged legacy field is what the edit form clears on save;
            # leaving it behind would let it out-rank the value approved.
            # LEGACY_MUSIC_SPLIT has already refused a write that would drop
            # the entertainment half of it; this clear is safe by then.
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
        if field_row.field != FOOD_ORDER_FIELD and field_row.edited_before_approval:
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
    # An approval is a person putting these words on the document -- they
    # read them, sometimes edited them, and chose to apply them -- so the
    # fields it changes are recorded as authored. The event type is a
    # separate question and stays "beo_proposal_applied": the regenerate
    # screen reads "document_edited" as "somebody typed into this version",
    # and an approval is not that.
    document = documents_service.update_content_fields(
        db,
        document,
        changes,
        actor=actor,
        event_type="beo_proposal_applied",
        authored_fields=document_regeneration.PROTECTED_FIELD_NAMES,
        placeholders=document_regeneration.GENERATED_PLACEHOLDERS,
    )
    if approved_food_lines is not None:
        # The Event Order is written and committed; now the money it
        # commits to, from the same lines (Aaron, 2026-09-11).
        sync_final_invoice_from_food(db, document.booking, approved_food_lines, actor=actor)
    return document


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
    _check_on_approval(document, {field_row.field: applied}, current_values(document))
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
        document, {row.field: applied for row, applied in decisions}, current_values(document)
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
        if field_row.field == FOOD_ORDER_FIELD:
            # Priced NOW from the catalogue, so the reviewer sees what an
            # approval would write. EVERY proposed line gets a row --
            # including one that can no longer be priced, which carries no
            # price and has to be set to 0 before the approval will go
            # through. Rendering only the priceable ones let the form post
            # a shorter selection and drop the rest silently (review,
            # 2026-09-11).
            selection = _load_selection(field_row.proposed_value)
            booking = proposal.booking
            before = current_food_lines(document)
            lines, problems = resolve_food_selection(
                db, booking, selection, already_ordered=ordered_item_ids(document)
            )
            priced = {ln["menu_item_id"]: ln for ln in lines}
            rendered = []
            for entry in selection:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("menu_item_id"))
                line = priced.get(item_id)
                if line is not None:
                    rendered.append(
                        {**line, "line_total": f"{Decimal(line['unit_price']) * line['quantity']:.2f}", "priceable": True}
                    )
                    continue
                retired = catalogue.get_by_id_any(db, item_id) if entry.get("menu_item_id") else None
                rendered.append(
                    {
                        "menu_item_id": item_id,
                        "name": retired.name if retired is not None else (entry.get("name") or "an item no longer in the catalogue"),
                        "category": retired.category.value if retired is not None else "",
                        "quantity": entry.get("quantity"),
                        "unit_price": None,
                        "line_total": None,
                        "priceable": False,
                    }
                )
            rows.append(
                {
                    "field": FOOD_ORDER_FIELD,
                    "kind": "food",
                    "label": beo_rules.FOOD_ORDER_LABEL,
                    "id": field_row.id,
                    "proposed": food_text(lines),
                    "lines": rendered,
                    "total": f"{food_total(lines):.2f}",
                    "problems": [v.message for v in problems],
                    "current": food_text(before),
                    "current_lines": [{**ln, "line_total": f"{Decimal(ln['unit_price']) * ln['quantity']:.2f}"} for ln in before],
                    "current_total": f"{food_total(before):.2f}" if before else None,
                    "replaces_text": bool(before),
                }
            )
            continue
        existing = current[field_row.field]
        rows.append(
            {
                "field": field_row.field,
                "kind": "text",
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
