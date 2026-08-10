"""Booking creation and status transitions. Every write here also appends
to booking_events — that's the whole point of the audit log: state changes
must never happen without leaving a trace of who/what/when/old->new.
"""

import datetime as dt
import secrets
import string
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Space
from app.models.booking import BookingStatus

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
