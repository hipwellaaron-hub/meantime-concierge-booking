"""Guided Booking Wizard: session lifecycle, per-step resumable saves, and
the staff eligibility worklist. Every write here also appends a
BookingEvent, matching every other state-changing service in this
codebase -- no wizard action should require re-reading an email chain (or
in this case, re-reading the wizard itself) to explain.

BEO/invoice generation is NOT part of this module -- submit_review() is
the single, explicitly marked hook where that attaches in a later phase.
"""

import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Document, Invoice, Space, Venue
from app.models.booking import BLOCKING_STATUSES
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.menu_item import MenuItemCategory
from app.models.wizard_session import WizardSession, WizardSessionStatus, WizardStep
from app.services import catalogue
from app.services.policy import WIZARD_TOKEN_TTL_DAYS, WIZARD_TRIGGER_DAYS_BEFORE_EVENT
from app.services.validation import (
    SETUP_ACCESS_STANDARD_TIME,
    ValidationWarning,
    validate_booking_time,
    validate_setup_access_time,
    validate_trading_hours,
)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


class BarStructure(str, enum.Enum):
    cash_bar = "cash_bar"
    bar_tab = "bar_tab"
    hybrid = "hybrid"


class MusicType(str, enum.Enum):
    own_playlist = "own_playlist"
    dj = "dj"


class CakeChoiceType(str, enum.Enum):
    in_house = "in_house"
    outside = "outside"
    none = "none"


# --- session lifecycle -------------------------------------------------


def get_or_create_session(db: Session, booking: Booking, *, actor: str) -> WizardSession:
    """Idempotent: a booking already carrying a live session gets that one
    back rather than a duplicate (booking_id is unique on this table)."""
    existing = db.execute(select(WizardSession).where(WizardSession.booking_id == booking.id)).scalar_one_or_none()
    if existing is not None:
        return existing

    session = WizardSession(
        booking_id=booking.id,
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=WIZARD_TOKEN_TTL_DAYS),
    )
    db.add(session)
    db.flush()

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="wizard_created",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(session)
    return session


def get_by_token(db: Session, token: str) -> WizardSession | None:
    return db.execute(select(WizardSession).where(WizardSession.access_token == token)).scalar_one_or_none()


def is_usable(session: WizardSession, *, now: dt.datetime | None = None) -> bool:
    """False for a revoked session, or one that expired without ever being
    submitted. A submitted session stays usable (read-only) even past
    expires_at, same as a signed document's link keeps resolving."""
    if session.status == WizardSessionStatus.revoked:
        return False
    if session.status == WizardSessionStatus.submitted:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return session.expires_at > now


def revoke_session(db: Session, session: WizardSession, *, actor: str) -> WizardSession:
    db.refresh(session, with_for_update=True)
    if session.status == WizardSessionStatus.submitted:
        raise ValueError("cannot revoke a submitted wizard session")
    session.status = WizardSessionStatus.revoked
    session.revoked_at = dt.datetime.now(dt.timezone.utc)
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_revoked", actor=actor))
    db.commit()
    db.refresh(session)
    return session


# --- staff eligibility worklist -----------------------------------------


def get_wizard_eligible_bookings(db: Session, venue: Venue, *, as_of: dt.date | None = None) -> list[Booking]:
    """Bookings that have crossed the 14-day-before-event mark, with a
    paid deposit and a signed (current) agreement -- matching the Master
    Policy BEO gating checklist -- and no wizard session already submitted.
    Mirrors app.services.ivvy_import.get_unassigned_bookings: a query only,
    not wired to any sending mechanism (no scheduler exists in this app)."""
    as_of = as_of or dt.date.today()
    cutoff = as_of + dt.timedelta(days=WIZARD_TRIGGER_DAYS_BEFORE_EVENT)

    deposit_paid = select(Invoice.booking_id).where(
        Invoice.type == InvoiceType.deposit, Invoice.status == InvoiceStatus.paid
    )
    agreement_signed = select(Document.booking_id).where(
        Document.type == DocumentType.agreement,
        Document.status == DocumentStatus.signed,
        Document.is_current.is_(True),
    )
    already_submitted = select(WizardSession.booking_id).where(WizardSession.status == WizardSessionStatus.submitted)

    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Booking.event_date >= as_of,
                Booking.event_date <= cutoff,
                Booking.status.in_(BLOCKING_STATUSES),
                Booking.id.in_(deposit_paid),
                Booking.id.in_(agreement_signed),
                Booking.id.notin_(already_submitted),
            )
            .order_by(Booking.event_date)
        ).all()
    )


# --- per-step saves ------------------------------------------------------


_STEP_ORDER = list(WizardStep)


def _advance_step(session: WizardSession, completed_step: WizardStep) -> None:
    """Forward-only: re-POSTing an earlier step (editing an answer already
    given) must never regress current_step back past where the client had
    already reached."""
    completed_index = _STEP_ORDER.index(completed_step)
    current_index = _STEP_ORDER.index(session.current_step)
    if current_index <= completed_index:
        session.current_step = _STEP_ORDER[min(completed_index + 1, len(_STEP_ORDER) - 1)]
    if session.status == WizardSessionStatus.pending:
        session.status = WizardSessionStatus.in_progress


ALREADY_SUBMITTED_MESSAGE = "this wizard has already been submitted and can no longer be edited"


def _lock_and_guard_editable(db: Session, session: WizardSession) -> None:
    # Locks the row for the rest of this transaction, then checks status --
    # same order as documents_service.sign / invoicing.record_payment. The
    # case this guards: a double-clicked submit racing a step edit, or two
    # submits, must not both pass a stale in-Python status check.
    db.refresh(session, with_for_update=True)
    if session.status == WizardSessionStatus.submitted:
        raise ValueError(ALREADY_SUBMITTED_MESSAGE)


def save_basics_step(
    db: Session,
    session: WizardSession,
    *,
    start_time: dt.time,
    end_time: dt.time,
    food_service_time: dt.time,
    setup_access_time: dt.time,
    adult_count: int,
    child_count: int,
    actor: str,
) -> list[ValidationWarning]:
    _lock_and_guard_editable(db, session)
    booking = session.booking

    changes = {
        "start_time": start_time,
        "end_time": end_time,
        "food_service_time": food_service_time,
        "setup_access_time": setup_access_time,
        "adult_count": adult_count,
        "child_count": child_count,
    }
    for field, new_value in changes.items():
        old_value = getattr(booking, field)
        if old_value != new_value:
            db.add(
                BookingEvent(
                    booking_id=booking.id,
                    event_type="wizard_step_saved",
                    field_name=field,
                    old_value=str(old_value) if old_value is not None else None,
                    new_value=str(new_value),
                    actor=actor,
                )
            )
            setattr(booking, field, new_value)

    # Standard-or-later requests are auto-confirmed; earlier ones are a
    # request pending Aaron -- never auto-promised. See app.models.booking.
    booking.setup_access_confirmed = setup_access_time >= SETUP_ACCESS_STANDARD_TIME

    warnings = validate_booking_time(booking.event_date, start_time, end_time)
    warnings += validate_setup_access_time(setup_access_time)
    warnings += validate_trading_hours(booking.event_date, end_time)

    _advance_step(session, WizardStep.basics)
    db.commit()
    db.refresh(session)
    db.refresh(booking)
    return warnings


def save_food_step(db: Session, session: WizardSession, *, platters: list[dict], pizzas: list[dict], actor: str) -> None:
    """Each item is {"menu_item_id": UUID, "quantity": int}. Validates every
    referenced item exists and is active -- JSONB can't be DB-FK-enforced,
    so this is the anti-tampering/correctness check for price-relevant
    client input."""
    _lock_and_guard_editable(db, session)
    booking = session.booking

    def _validate(items: list[dict], category) -> list[dict]:
        cleaned = []
        for item in items:
            menu_item_id = item["menu_item_id"]
            quantity = int(item["quantity"])
            if quantity <= 0:
                continue
            menu_item = catalogue.get_by_id(db, menu_item_id)
            if menu_item is None or menu_item.category != category:
                raise ValueError(f"unknown or inactive menu item {menu_item_id}")
            cleaned.append({"menu_item_id": str(menu_item_id), "quantity": quantity})
        return cleaned

    session.food_response = {
        "platters": _validate(platters, MenuItemCategory.platter),
        "pizzas": _validate(pizzas, MenuItemCategory.pizza),
    }
    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_step_saved", field_name="food", actor=actor))
    _advance_step(session, WizardStep.food)
    db.commit()
    db.refresh(session)


def save_beverage_step(
    db: Session,
    session: WizardSession,
    *,
    bar_structure: BarStructure,
    bar_limit: Decimal | None,
    bar_inclusions: str | None,
    actor: str,
) -> None:
    _lock_and_guard_editable(db, session)
    if bar_structure in (BarStructure.bar_tab, BarStructure.hybrid) and bar_limit is None:
        raise ValueError(f"{bar_structure.value} requires a dollar limit")

    session.beverage_response = {
        "bar_structure": bar_structure.value,
        "bar_limit": str(bar_limit) if bar_limit is not None else None,
        "bar_inclusions": bar_inclusions,
    }
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_step_saved", field_name="beverage", actor=actor))
    _advance_step(session, WizardStep.beverage)
    db.commit()
    db.refresh(session)


def save_music_step(
    db: Session, session: WizardSession, *, music_type: MusicType, notes: str | None, bump_in_notes: str | None, actor: str
) -> None:
    _lock_and_guard_editable(db, session)
    session.music_response = {"music_type": music_type.value, "notes": notes, "bump_in_notes": bump_in_notes}
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_step_saved", field_name="music", actor=actor))
    _advance_step(session, WizardStep.music)
    db.commit()
    db.refresh(session)


def save_extras_step(
    db: Session,
    session: WizardSession,
    *,
    cake_choice_type: CakeChoiceType,
    cake_menu_item_id: uuid.UUID | None,
    cake_notes: str | None,
    decorations_notes: str | None,
    layout_notes: str | None,
    dietary_requirements: str | None,
    accessibility_needs: str | None,
    additional_notes: str | None,
    actor: str,
) -> bool:
    """Returns True if this save triggered a hard accessibility escalation."""
    _lock_and_guard_editable(db, session)
    booking = session.booking

    if cake_choice_type == CakeChoiceType.outside and not booking.outside_cake_permitted:
        raise ValueError("outside cake is not permitted on this booking")
    if cake_choice_type == CakeChoiceType.in_house and cake_menu_item_id is not None:
        menu_item = catalogue.get_by_id(db, cake_menu_item_id)
        if menu_item is None or menu_item.category != MenuItemCategory.cake:
            raise ValueError(f"unknown or inactive cake item {cake_menu_item_id}")

    session.extras_response = {
        "cake_choice": {
            "type": cake_choice_type.value,
            "menu_item_id": str(cake_menu_item_id) if cake_menu_item_id is not None else None,
            "notes": cake_notes,
        },
        "decorations_notes": decorations_notes,
        "layout_notes": layout_notes,
        "dietary_requirements": dietary_requirements,
        "accessibility_needs": accessibility_needs,
        "additional_notes": additional_notes,
    }
    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_step_saved", field_name="extras", actor=actor))

    escalated = False
    accessibility_flag = bool(accessibility_needs and accessibility_needs.strip())
    if accessibility_flag and not booking.space.wheelchair_accessible:
        escalated = flag_accessibility_escalation(db, session, actor=actor)

    _advance_step(session, WizardStep.extras)
    db.commit()
    db.refresh(session)
    return escalated


def flag_accessibility_escalation(db: Session, session: WizardSession, *, actor: str) -> bool:
    """Master Policy v1.3 §1.2: raising accessibility against a Loft/
    Mezzanine booking "is an escalation, not a note" -- it may mean the
    booking cannot proceed in the booked space. Distinct event_type from
    ordinary wizard_step_saved so it's independently queryable, and a
    materialized has_hard_escalation flag for a future dashboard view."""
    if session.has_hard_escalation:
        return True  # already flagged, idempotent
    session.has_hard_escalation = True
    db.add(
        BookingEvent(
            booking_id=session.booking_id,
            event_type="accessibility_escalation",
            actor=actor,
            new_value="accessibility need raised against a non-accessible space -- route to Aaron immediately",
        )
    )
    return True


def submit_review(db: Session, session: WizardSession, *, actor: str) -> WizardSession:
    _lock_and_guard_editable(db, session)
    session.status = WizardSessionStatus.submitted
    session.submitted_at = dt.datetime.now(dt.timezone.utc)
    session.current_step = WizardStep.review
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_submitted", actor=actor))

    # HOOK: BEO regeneration + final invoice generation attach here in a
    # later phase (not designed yet). Both gated OFF by default via
    # Settings.wizard_beo_auto_finalize / Settings.wizard_invoice_auto_send
    # (see app/config.py) -- the BEO is an internal operational document to
    # staff, the invoice is client-facing, and those may warrant different
    # auto-route rules, so they're independently switchable.

    db.commit()
    db.refresh(session)
    return session
