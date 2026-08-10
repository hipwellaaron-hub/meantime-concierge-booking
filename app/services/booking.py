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


def generate_reference_code(db: Session, event_date: dt.date, venue_slug: str = "HAM") -> str:
    """Human-readable and unique. Retries on the rare random collision
    rather than relying on any external counter."""
    for _ in range(10):
        suffix = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_SUFFIX_LENGTH))
        code = f"{venue_slug.upper()}-{event_date:%Y%m%d}-{suffix}"
        exists = db.execute(select(Booking.id).where(Booking.reference_code == code)).first()
        if exists is None:
            return code
    raise RuntimeError("Could not generate a unique reference code after 10 attempts")


def create_booking(
    db: Session,
    *,
    space_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    event_date: dt.date,
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


def change_status(db: Session, booking: Booking, new_status: BookingStatus, *, actor: str) -> Booking:
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
    db.commit()
    db.refresh(booking)
    return booking


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
    db: Session, booking: Booking, *, space_id: uuid.UUID, start_time: dt.time, end_time: dt.time, actor: str
) -> Booking:
    """Triage action for iVvy-imported bookings, which land in the
    placeholder Unassigned space with no time-of-day (see
    app/services/ivvy_import.py). Moving to a real, bookable space is what
    actually subjects the booking to the double-booking exclusion
    constraint for the first time -- a genuine overlap raises
    IntegrityError, which the caller (app/api/admin_bookings.py) turns
    into a 409, same as any other constraint violation in this app."""
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")

    old_space_id, old_start, old_end = booking.space_id, booking.start_time, booking.end_time
    booking.space_id = space_id
    booking.start_time = start_time
    booking.end_time = end_time
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
