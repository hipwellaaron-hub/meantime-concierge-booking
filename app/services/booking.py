"""Booking creation and status transitions. Every write here also appends
to booking_events — that's the whole point of the audit log: state changes
must never happen without leaving a trace of who/what/when/old->new.
"""

import datetime as dt
import secrets
import string
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Contact, Space
from app.models.booking import BookingStatus, MinReductionReasonCode

REFERENCE_ALPHABET = string.ascii_uppercase + string.digits
REFERENCE_SUFFIX_LENGTH = 5

# Full-day span used when a hold is created with no specific time. A NULL
# start/end time produces a NULL time_range (see app/models/booking.py's
# comment on the Computed column), and a NULL time_range never conflicts
# under the exclusion constraint -- so an untimed "block the whole day"
# hold would look correctly occupied on the calendar without actually
# being protected against a real double-booking. Pinning it to a concrete
# span keeps it genuinely behind the same database constraint that
# protects every other blocking booking. 11:30pm matches the existing
# music-off-time boundary (app.services.validation.MUSIC_OFF_TIME) as the
# latest reasonable end of day.
HOLD_FULL_DAY_START = dt.time(9, 0)
HOLD_FULL_DAY_END = dt.time(23, 30)

# The booking lifecycle. Terminal statuses (completed, cancelled, dead) map
# to an empty tuple -- no further transition is legal from there. This is
# enforced only by transition_status() below, not by change_status() itself:
# change_status() stays an unchecked primitive because existing one-off
# scripts and test fixtures already jump straight to a status (e.g.
# enquiry -> confirmed) for setup convenience, and must keep working.
LEGAL_TRANSITIONS: dict[BookingStatus, tuple[BookingStatus, ...]] = {
    BookingStatus.enquiry: (BookingStatus.offered, BookingStatus.dead),
    BookingStatus.offered: (BookingStatus.tentative, BookingStatus.dead),
    BookingStatus.tentative: (BookingStatus.confirmed, BookingStatus.cancelled),
    BookingStatus.confirmed: (BookingStatus.completed, BookingStatus.cancelled),
    BookingStatus.completed: (),
    BookingStatus.cancelled: (),
    BookingStatus.dead: (),
}


def generate_reference_code(db: Session, event_date: dt.date | None, venue_slug: str = "HAM") -> str:
    """Human-readable and unique. Retries on the rare random collision
    rather than relying on any external counter. event_date is None for
    an enquiry that arrived with no date locked in yet -- "TBD" stands in
    for the date segment rather than guessing one."""
    date_part = f"{event_date:%Y%m%d}" if event_date is not None else "TBD"
    for _ in range(10):
        suffix = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_SUFFIX_LENGTH))
        code = f"{venue_slug.upper()}-{date_part}-{suffix}"
        exists = db.execute(select(Booking.id).where(Booking.reference_code == code)).first()
        if exists is None:
            return code
    raise RuntimeError("Could not generate a unique reference code after 10 attempts")


def create_booking(
    db: Session,
    *,
    space_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    event_date: dt.date | None,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    proposed_time_slot: str | None = None,
    event_name: str,
    event_type: str | None,
    adult_count: int,
    child_count: int,
    notes: str | None,
    actor: str,
    status: BookingStatus = BookingStatus.enquiry,
    lead_source: str | None = None,
    lead_referrer: str | None = None,
    migration_source: str | None = None,
    migration_external_ref: str | None = None,
    migration_snapshot: dict | None = None,
    agreed_min_adults: int | None = None,
) -> Booking:
    space = db.get(Space, space_id)
    if space is None:
        raise ValueError(f"Unknown space {space_id}")

    booking = Booking(
        space_id=space_id,
        contact_id=contact_id,
        event_date=event_date,
        start_time=start_time,
        end_time=end_time,
        proposed_time_slot=proposed_time_slot,
        event_name=event_name,
        event_type=event_type,
        adult_count=adult_count,
        child_count=child_count,
        notes=notes,
        status=status,
        reference_code=generate_reference_code(db, event_date),
        lead_source=lead_source,
        lead_referrer=lead_referrer,
        migration_source=migration_source,
        migration_external_ref=migration_external_ref,
        migration_snapshot=migration_snapshot,
        # Defaults to the space's standard minimum -- "the agreed minimum
        # defaults to the standard" (Master Policy v1.3 §4.1). Only ever
        # changes from here via an explicit staff reduction afterward.
        agreed_min_adults=agreed_min_adults if agreed_min_adults is not None else space.standard_min_adults,
    )
    db.add(booking)
    db.flush()  # assigns booking.id, and is where the exclusion constraint fires

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="created",
            actor=actor,
            new_value=status.value,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def change_status(
    db: Session, booking: Booking, new_status: BookingStatus, *, actor: str, reason: str | None = None
) -> Booking:
    """Unchecked primitive -- sets status and logs it, no legality check.
    Staff-facing transitions must go through transition_status() instead,
    which validates against LEGAL_TRANSITIONS before calling this."""
    old_status = booking.status
    if old_status == new_status:
        return booking

    booking.status = new_status
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="status_changed",
            field_name="status",
            old_value=old_status.value,
            new_value=new_status.value,
            actor=actor,
        )
    )
    if reason:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="status_changed",
                field_name="status_change_reason",
                new_value=reason,
                actor=actor,
            )
        )
    db.commit()
    db.refresh(booking)
    return booking


def transition_status(
    db: Session, booking: Booking, new_status: BookingStatus, *, actor: str, reason: str | None = None
) -> Booking:
    """The validated, staff-facing entry point for moving a booking through
    its lifecycle. Enforces LEGAL_TRANSITIONS; raises ValueError (-> 422 at
    the API layer) on an illegal move rather than allowing it silently.
    Re-submitting the current status is treated as a harmless no-op (e.g. a
    double form submission) rather than an illegal transition."""
    if new_status == booking.status:
        return change_status(db, booking, new_status, actor=actor, reason=reason)
    legal = LEGAL_TRANSITIONS.get(booking.status, ())
    if new_status not in legal:
        allowed = ", ".join(s.value for s in legal) or "none -- this is a terminal status"
        raise ValueError(f"Cannot move from '{booking.status.value}' to '{new_status.value}'. Allowed: {allowed}.")
    return change_status(db, booking, new_status, actor=actor, reason=reason)


def search_bookings(
    db: Session, venue_id: uuid.UUID, *, status: BookingStatus | None = None, query: str | None = None, limit: int = 100
) -> list[Booking]:
    stmt = select(Booking).join(Space, Booking.space_id == Space.id).where(Space.venue_id == venue_id)
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if query and query.strip():
        like = f"%{query.strip()}%"
        stmt = stmt.outerjoin(Contact, Booking.contact_id == Contact.id).where(
            or_(Booking.event_name.ilike(like), Booking.reference_code.ilike(like), Contact.name.ilike(like))
        )
    return list(db.scalars(stmt.order_by(Booking.event_date.desc()).limit(limit)).all())


def confirm_setup_access(db: Session, booking: Booking, *, actor: str) -> Booking:
    """Master Policy v1.3 §1.8: setup access earlier than the 2pm standard
    'must be confirmed, never promised' -- this is that confirmation,
    staff-only. setup_access_confirmed is tri-state (see app/models/booking.py):
    False means a genuine pending request, so this only makes sense there."""
    if booking.setup_access_confirmed is not False:
        raise ValueError("no pending early setup-access request to confirm")
    booking.setup_access_confirmed = True
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="setup_access_confirmed",
            old_value="False",
            new_value="True",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def set_agreed_minimum(
    db: Session, booking: Booking, *, agreed_min_adults: int, reason: MinReductionReasonCode | None, actor: str
) -> Booking:
    """Master Policy v1.3 §4: 'changes only on Aaron's explicit approval...
    logged with who, when, and why... never set silently.' A reason is
    required whenever this differs from the space's own standard minimum
    (Space.standard_min_adults, never mutated here) and cleared once it's
    reset back to standard, so a stale reason never lingers."""
    if agreed_min_adults != booking.space.standard_min_adults and reason is None:
        raise ValueError("a reason is required when the agreed minimum differs from the space standard")
    if agreed_min_adults == booking.space.standard_min_adults:
        reason = None

    old_min, old_reason = booking.agreed_min_adults, booking.agreed_min_reduction_reason
    booking.agreed_min_adults = agreed_min_adults
    booking.agreed_min_reduction_reason = reason
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="agreed_min_adults",
            old_value=str(old_min),
            new_value=str(agreed_min_adults),
            actor=actor,
        )
    )
    if reason != old_reason:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="agreed_min_reduction_reason",
                old_value=old_reason.value if old_reason else None,
                new_value=reason.value if reason else None,
                actor=actor,
            )
        )
    db.commit()
    db.refresh(booking)
    return booking


def assign_space_and_time(
    db: Session,
    booking: Booking,
    *,
    space_id: uuid.UUID,
    start_time: dt.time,
    end_time: dt.time,
    event_date: dt.date | None = None,
    actor: str,
) -> Booking:
    """Triage action for iVvy-imported bookings, which land in the
    placeholder Unassigned space with no time-of-day (see
    app/services/ivvy_import.py). Moving to a real, bookable space is what
    actually subjects the booking to the double-booking exclusion
    constraint for the first time -- a genuine overlap raises
    IntegrityError, which the caller (app/api/admin_bookings.py) turns
    into a 409, same as any other constraint violation in this app.

    event_date is optional and only touched when given: an enquiry that
    arrived with a real date needs it left alone here, while one that
    arrived with no date (see app.services.enquiry_classification's
    missing_event_date flag) needs a place for staff to record it once
    they've actually spoken to the client -- this is that place, reusing
    the same triage action rather than adding a whole separate one.

    Also reconciles agreed_min_adults when it's still sitting at the old
    space's standard with no reduction reason recorded -- i.e. it was never
    deliberately touched. The placeholder Unassigned space always has
    standard_min_adults=0, so every freshly-imported booking starts there;
    left alone, moving it into a real space would leave a stale 0 with no
    reason, which looks exactly like an unrecorded silent reduction (see
    get_bookings_with_unrecorded_minimum_reduction). A genuinely
    staff-set custom minimum (one that already differs from the old
    space's standard) is left untouched."""
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")

    old_space = booking.space
    old_space_id, old_start, old_end = booking.space_id, booking.start_time, booking.end_time
    if (
        space_id != old_space_id
        and booking.agreed_min_adults == old_space.standard_min_adults
        and booking.agreed_min_reduction_reason is None
        and space.standard_min_adults != booking.agreed_min_adults
    ):
        old_min = booking.agreed_min_adults
        booking.agreed_min_adults = space.standard_min_adults
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="agreed_min_adults",
                old_value=str(old_min),
                new_value=str(space.standard_min_adults),
                actor=actor,
            )
        )
    booking.space_id = space_id
    booking.start_time = start_time
    booking.end_time = end_time
    if event_date is not None and event_date != booking.event_date:
        old_event_date = booking.event_date
        booking.event_date = event_date
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="event_date",
                old_value=str(old_event_date) if old_event_date else None,
                new_value=str(event_date),
                actor=actor,
            )
        )
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="space_id",
            old_value=str(old_space_id),
            new_value=str(space_id),
            actor=actor,
        )
    )
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="start_time/end_time",
            old_value=f"{old_start}-{old_end}",
            new_value=f"{start_time}-{end_time}",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def set_outside_cake_permitted(db: Session, booking: Booking, *, permitted: bool, actor: str) -> Booking:
    """Staff-only grandfathering per Master Policy v1.3 §1.6 -- new
    bookings default False; only Aaron can flip this for an existing
    client he's already agreed with."""
    old = booking.outside_cake_permitted
    if old == permitted:
        return booking
    booking.outside_cake_permitted = permitted
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="outside_cake_permitted",
            old_value=str(old),
            new_value=str(permitted),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def get_bookings_with_unrecorded_minimum_reduction(db: Session, venue_id: uuid.UUID) -> list[Booking]:
    """Master Policy v1.3 SS4.4, enforceable version: agreed_min_adults
    should never differ from the space's own standard without a recorded
    reason. set_agreed_minimum() enforces this for every change made
    through the dashboard, but create_booking() accepts an explicit
    agreed_min_adults with no reason check (imports, migration, scripts) --
    this surfaces that drift for staff to close out rather than silently
    charging a shortfall nobody agreed to."""
    open_statuses = (BookingStatus.enquiry, BookingStatus.offered, BookingStatus.tentative, BookingStatus.confirmed)
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue_id,
                Booking.agreed_min_adults != Space.standard_min_adults,
                Booking.agreed_min_reduction_reason.is_(None),
                Booking.status.in_(open_statuses),
            )
            .order_by(Booking.created_at)
        ).all()
    )


def create_hold(
    db: Session,
    *,
    space_id: uuid.UUID,
    event_date: dt.date,
    event_name: str,
    contact_id: uuid.UUID | None = None,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    hold_expires_at: dt.date | None = None,
    actor: str,
) -> Booking:
    """A hold is a Booking created directly at 'tentative' status --
    skipping the normal enquiry -> offered pipeline entirely, for the
    "block this date for a client, no formal enquiry exists yet" case.
    Gets the exact same double-booking protection as any other tentative
    booking (see LEGAL_TRANSITIONS and the exclusion constraint in
    app/models/booking.py) because it IS one, not a parallel concept.
    hold_expires_at is optional -- a hold with none is deliberately
    open-ended, not a bug."""
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")

    booking = create_booking(
        db,
        space_id=space_id,
        contact_id=contact_id,
        event_date=event_date,
        start_time=start_time if start_time is not None else HOLD_FULL_DAY_START,
        end_time=end_time if end_time is not None else HOLD_FULL_DAY_END,
        event_name=event_name,
        event_type=None,
        adult_count=0,
        child_count=0,
        notes=None,
        actor=actor,
        status=BookingStatus.tentative,
    )
    if hold_expires_at is not None:
        booking.hold_expires_at = hold_expires_at
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="hold_expires_at",
                new_value=str(hold_expires_at),
                actor=actor,
            )
        )
        db.commit()
        db.refresh(booking)
    return booking


def set_hold_expiry(db: Session, booking: Booking, *, hold_expires_at: dt.date | None, actor: str) -> Booking:
    old = booking.hold_expires_at
    if old == hold_expires_at:
        return booking
    booking.hold_expires_at = hold_expires_at
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="hold_expires_at",
            old_value=str(old) if old else None,
            new_value=str(hold_expires_at) if hold_expires_at else None,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking
