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
from app.services import catalogue, wizard_generation
from app.services.food_guidance import FoodGuidance, generate_food_guidance
from app.services.policy import WIZARD_TOKEN_TTL_DAYS, WIZARD_TRIGGER_DAYS_BEFORE_EVENT
from app.services.validation import (
    SETUP_ACCESS_STANDARD_TIME,
    ValidationWarning,
    validate_booking_time,
    validate_setup_access_time,
    validate_trading_hours,
)
from app.utils import is_valid_email

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


class BarStructure(str, enum.Enum):
    cash_bar = "cash_bar"
    bar_tab = "bar_tab"
    hybrid = "hybrid"


class MusicType(str, enum.Enum):
    own_playlist = "own_playlist"
    dj = "dj"
    # Arranged by the venue at the same rate as a DJ.
    musician = "musician"


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

    if booking.parent_booking_id is not None:
        # Same reasoning as app.services.documents.create_new_version: a
        # linked child (see app.services.booking.add_linked_space) is a
        # second room for the parent's event -- the wizard belongs on the
        # parent, which is the booking the client actually has a
        # relationship with.
        raise ValueError("cannot send a wizard link for a linked booking -- use the parent booking instead")

    if booking.event_date is None:
        # The wizard's own weekday-dependent checks (trading hours, setup
        # access) have nothing to check against without one -- confirm the
        # date on the booking first (see the "Space & time" card).
        raise ValueError("cannot send a wizard link before the event date is confirmed")

    # Same "don't hand out a link with nowhere to send it" rule as
    # app.services.documents.mark_sent / app.services.invoicing.mark_sent.
    contact = booking.contact
    if contact is None or not is_valid_email(contact.email):
        raise ValueError("cannot send a wizard link: this booking has no contact with a valid email address on file")

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


def record_open(db: Session, session: WizardSession) -> WizardSession:
    """Called on the client's first GET of the wizard link. Sets
    opened_at once, if not already set -- status alone can't tell "never
    opened" apart from "opened, but closed before saving a step" (both
    sit at pending/basics until save_basics_step's editable-guard flips
    status to in_progress)."""
    if session.opened_at is None:
        session.opened_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(session)
    return session


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


# Canonical order = WizardStep definition order. Individual bookings get
# a subset via step_order_for -- indices for forward-only comparisons are
# always taken against THIS list, so a step that doesn't exist for some
# booking still has a stable position.
_CANONICAL_STEP_ORDER = list(WizardStep)


def step_order_for(booking) -> list[WizardStep]:
    """The steps this booking's wizard actually presents. The AV step
    exists only for The Loft -- the screen is physically in that room, so
    asking a Mezzanine client about USB slideshows would be asking about
    equipment their event doesn't have."""
    order = [
        WizardStep.basics,
        WizardStep.food,
        WizardStep.beverage,
        WizardStep.music,
        WizardStep.vendors,
        WizardStep.extras,
    ]
    if booking.space.name == "The Loft":
        order.append(WizardStep.av)
    order.append(WizardStep.review)
    return order


def _advance_step(session: WizardSession, completed_step: WizardStep) -> None:
    """Forward-only: re-POSTing an earlier step (editing an answer already
    given) must never regress current_step back past where the client had
    already reached.

    Comparisons run on canonical indices, but the step actually advanced
    TO comes from the booking's own order -- so a session stranded on a
    step its booking no longer has (e.g. current_step=av after the space
    changed away from The Loft) neither crashes nor advances into another
    step that doesn't exist."""
    order = step_order_for(session.booking)
    completed_index = _CANONICAL_STEP_ORDER.index(completed_step)
    current_index = _CANONICAL_STEP_ORDER.index(session.current_step)
    if current_index <= completed_index:
        next_steps = [s for s in order if _CANONICAL_STEP_ORDER.index(s) > completed_index]
        session.current_step = next_steps[0] if next_steps else order[-1]
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
    guest_arrival_time: dt.time | None = None,
    key_moments: list[dict] | None = None,
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
        # Client-known timeline facts (see the Event Order's timeline):
        # when guests actually arrive, and the run-sheet moments --
        # speeches, cake cutting, raffle draws. An empty moments list
        # normalizes to None so an untouched field doesn't log a
        # spurious None -> [] change on every save.
        "guest_arrival_time": guest_arrival_time,
        "key_moments": key_moments or None,
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

    # ALWAYS a request pending staff confirmation, never auto-promised --
    # same semantics as a vendor's bump-in time (per Aaron, 28 Aug 2026:
    # "setup access is a request, not a confirmation"; previously
    # standard-or-later times auto-confirmed). Staff confirm via
    # app.services.booking.confirm_setup_access, and the Event Order
    # renders the requested/confirmed qualifier either way.
    booking.setup_access_confirmed = False

    warnings = validate_booking_time(booking.event_date, start_time, end_time)
    warnings += validate_setup_access_time(setup_access_time)
    warnings += validate_trading_hours(booking.event_date, end_time)

    _advance_step(session, WizardStep.basics)
    db.commit()
    db.refresh(session)
    db.refresh(booking)
    return warnings


def save_food_step(
    db: Session,
    session: WizardSession,
    *,
    platters: list[dict],
    pizzas: list[dict],
    sides: list[dict] | None = None,
    desserts: list[dict] | None = None,
    actor: str,
) -> FoodGuidance:
    """Each item is {"menu_item_id": UUID, "quantity": int}. Validates every
    referenced item exists and is active -- JSONB can't be DB-FK-enforced,
    so this is the anti-tampering/correctness check for price-relevant
    client input. Also resolves each item's real price (current vs.
    legacy) and returns advisory FoodGuidance -- see app.services.food_guidance.

    The subtotal stored here is for wizard-display guidance ONLY. BEO/
    invoice generation (a later phase) must always recompute fresh from
    the catalogue at generation time, never trust this stored figure --
    catalogue prices can change between when a client fills the wizard and
    when staff finalize the booking.
    """
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
            cleaned.append({"menu_item_id": str(menu_item_id), "quantity": quantity, "menu_item": menu_item})
        return cleaned

    validated_platters = _validate(platters, MenuItemCategory.platter)
    validated_pizzas = _validate(pizzas, MenuItemCategory.pizza)
    validated_sides = _validate(sides or [], MenuItemCategory.side)
    validated_desserts = _validate(desserts or [], MenuItemCategory.dessert)

    subtotal = Decimal("0.00")
    needs_price_review: list[str] = []
    for entry in validated_platters + validated_pizzas + validated_sides + validated_desserts:
        price = catalogue.resolve_price(entry["menu_item"], booking)
        if price is None:
            # A legacy-eligible booking selected an item with no defined
            # legacy price (e.g. Vegetarian Pizza, new in v1.3) -- never
            # guess a figure. Excluded from the guidance subtotal; staff
            # must confirm the real price at generation time.
            needs_price_review.append(entry["menu_item"].name)
            continue
        subtotal += price * entry["quantity"]

    session.food_response = {
        "platters": [{"menu_item_id": e["menu_item_id"], "quantity": e["quantity"]} for e in validated_platters],
        "pizzas": [{"menu_item_id": e["menu_item_id"], "quantity": e["quantity"]} for e in validated_pizzas],
        "sides": [{"menu_item_id": e["menu_item_id"], "quantity": e["quantity"]} for e in validated_sides],
        "desserts": [{"menu_item_id": e["menu_item_id"], "quantity": e["quantity"]} for e in validated_desserts],
        "guidance_subtotal": str(subtotal),
        "needs_price_review": needs_price_review,
    }

    total_platter_count = sum(e["quantity"] for e in validated_platters)
    total_guest_count = booking.adult_count + booking.child_count
    guidance = generate_food_guidance(
        subtotal=subtotal,
        min_food_spend=booking.space.min_food_spend,
        platter_count=total_platter_count,
        total_guest_count=total_guest_count,
    )

    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_step_saved", field_name="food", actor=actor))
    if needs_price_review:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_needs_review",
                field_name="food_pricing",
                new_value=f"no legacy price defined for: {', '.join(needs_price_review)}",
                actor=actor,
            )
        )
    _advance_step(session, WizardStep.food)
    db.commit()
    db.refresh(session)
    return guidance


def save_beverage_step(
    db: Session,
    session: WizardSession,
    *,
    bar_structure: BarStructure,
    bar_limit: Decimal | None,
    bar_inclusions: list[str] | str | None,
    bar_inclusions_note: str | None = None,
    actor: str,
) -> None:
    _lock_and_guard_editable(db, session)
    if bar_structure in (BarStructure.bar_tab, BarStructure.hybrid) and bar_limit is None:
        raise ValueError(f"{bar_structure.value} requires a dollar limit")

    session.beverage_response = {
        "bar_structure": bar_structure.value,
        "bar_limit": str(bar_limit) if bar_limit is not None else None,
        # A list (the inclusion checkboxes) for new saves; a plain string
        # on sessions saved before the checkboxes existed.
        "bar_inclusions": bar_inclusions,
        "bar_inclusions_note": bar_inclusions_note,
    }
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_step_saved", field_name="beverage", actor=actor))
    _advance_step(session, WizardStep.beverage)
    db.commit()
    db.refresh(session)


def save_music_step(
    db: Session,
    session: WizardSession,
    *,
    music_types: list[MusicType] | None = None,
    music_type: MusicType | None = None,
    notes: str | None,
    bump_in_notes: str | None,
    actor: str,
) -> None:
    """Multi-select: a playlist before/after a DJ set is a normal event,
    so playlist/DJ/musician are not mutually exclusive. The legacy single
    music_type param is still accepted (older callers/tests) and coerced
    to a one-item list; the stored response keeps a music_type key mirroring
    the first selection so older readers keep working."""
    _lock_and_guard_editable(db, session)
    selected = list(music_types) if music_types else ([music_type] if music_type else [])
    if not selected:
        raise ValueError("pick at least one music option")
    session.music_response = {
        "music_types": [m.value for m in selected],
        "music_type": selected[0].value,  # legacy mirror
        "notes": notes,
        "bump_in_notes": bump_in_notes,
    }
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_step_saved", field_name="music", actor=actor))
    _advance_step(session, WizardStep.music)
    db.commit()
    db.refresh(session)


def save_vendors_step(db: Session, session: WizardSession, *, vendors: list[dict], actor: str) -> None:
    """Each vendor: {"vendor_type", "name", "contact_number", "bump_in_time"
    ("HH:MM" or None)}. An empty list is a real answer ("no vendors"),
    distinct from a never-saved None -- the completeness check tests for
    None.

    Writes two places: vendors_response keeps the client's verbatim answer
    (audit + wizard resumability), and booking_vendors rows are synced as
    the authoritative record staff act on. Sync rules protect staff work
    from client re-saves:
    - wizard-sourced rows match on (vendor_type, name); a matched row with
      an UNCHANGED bump_in_time keeps its bump_in_confirmed -- but a
      changed time resets confirmation to pending (False), exactly the
      setup-access semantics: the previously confirmed time is no longer
      the time being requested.
    - wizard-sourced rows the client no longer lists are deleted.
    - staff-sourced rows are never touched by this function at all.
    """
    from app.models import BookingVendor

    _lock_and_guard_editable(db, session)
    booking = session.booking

    session.vendors_response = {"vendors": vendors}

    existing = {
        (v.vendor_type, v.name): v
        for v in db.scalars(
            select(BookingVendor).where(BookingVendor.booking_id == booking.id, BookingVendor.source == "wizard")
        )
    }
    seen: set[tuple[str, str]] = set()
    for entry in vendors:
        key = (entry["vendor_type"], entry["name"])
        seen.add(key)
        bump_in = dt.time.fromisoformat(entry["bump_in_time"]) if entry.get("bump_in_time") else None
        row = existing.get(key)
        if row is None:
            db.add(
                BookingVendor(
                    booking_id=booking.id,
                    vendor_type=entry["vendor_type"],
                    name=entry["name"],
                    contact_number=entry.get("contact_number"),
                    bump_in_time=bump_in,
                    # A nominated time starts as a REQUEST (False), never a
                    # confirmation -- clients routinely nominate a time
                    # their DJ hasn't agreed to. No time at all -> NULL.
                    bump_in_confirmed=False if bump_in is not None else None,
                    source="wizard",
                )
            )
        else:
            row.contact_number = entry.get("contact_number")
            if row.bump_in_time != bump_in:
                row.bump_in_time = bump_in
                row.bump_in_confirmed = False if bump_in is not None else None
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)

    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_step_saved", field_name="vendors", actor=actor))
    _advance_step(session, WizardStep.vendors)
    db.commit()
    db.refresh(session)


def save_av_step(
    db: Session,
    session: WizardSession,
    *,
    video_slideshow: bool,
    microphones_for_speeches: bool,
    notes: str | None,
    actor: str,
) -> None:
    """The Loft only -- the screen is physically in that room. The client
    UI already hides this step for other spaces, but the client is never
    trusted: a POST for a non-Loft booking is refused here regardless."""
    _lock_and_guard_editable(db, session)
    if session.booking.space.name != "The Loft":
        raise ValueError("the AV step does not apply to this booking's space")
    session.av_response = {
        "video_slideshow": video_slideshow,
        "microphones_for_speeches": microphones_for_speeches,
        "notes": notes,
    }
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_step_saved", field_name="av", actor=actor))
    _advance_step(session, WizardStep.av)
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


def submit_review(db: Session, session: WizardSession, *, actor: str, final_notes: str | None = None):
    """Returns (session, WizardGenerationResult) -- the generation result
    carries is_clean/outstanding_items/document/invoice for the caller
    (the /w/{token}/review router) to surface. See
    app.services.wizard_generation.generate_beo_and_invoice for the BEO/
    invoice generation and auto-route logic itself.

    final_notes is the review step's "anything else we should know?" box:
    the escape hatch for whatever the form didn't ask, so a client adds it
    here instead of abandoning or emailing separately. Stored on
    extras_response and surfaced in the BEO's Special Notes.
    """
    _lock_and_guard_editable(db, session)
    if final_notes and final_notes.strip():
        session.extras_response = {**(session.extras_response or {}), "final_notes": final_notes.strip()[:2000]}
    session.status = WizardSessionStatus.submitted
    session.submitted_at = dt.datetime.now(dt.timezone.utc)
    session.current_step = WizardStep.review
    db.add(BookingEvent(booking_id=session.booking_id, event_type="wizard_submitted", actor=actor))
    db.commit()
    db.refresh(session)

    generation_result = wizard_generation.generate_beo_and_invoice(db, session, actor=actor)

    db.refresh(session)
    return session, generation_result
