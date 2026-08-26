"""One-off maintenance operation: move every booking with a non-NULL
event_date on or before a cutoff date into the `archived` status.

Built for a specific real request: clearing pre-launch/iVvy-era bookings
ahead of a real go-live date, at the owner's explicit instruction, after
being shown exactly what that includes (real, non-test client bookings)
and after confirming a hard delete isn't possible -- booking_events is a
database-enforced append-only audit log (see app/models/booking_event.py),
so a booking can never be fully erased while it has one, and every
booking has at least its own "created" event. Archiving instead of
deleting works with that guarantee rather than against it: the full
history stays intact and reversible (every status change here is logged
the same as any other, with the previous status recorded), it just stops
counting as live -- frees the date for real booking, drops off every
active worklist, and reads as clearly not a real cancellation on the
booking's own page.

Idempotent: an already-archived booking is never re-touched, so running
this twice for the same (or a later) cutoff only ever affects whatever's
newly in range.

CLI usage:
    ARCHIVE_CUTOFF_DATE=2026-09-30 ARCHIVE_CONFIRM=yes-archive-pre-launch-bookings \
        python -m app.archive_bookings_before

Both env vars are required, with no default for either -- this must
never do something this broad by accident from a bare invocation.
"""

import datetime as dt
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Booking
from app.models.booking import BookingStatus
from app.services.booking import change_status

REQUIRED_CONFIRM_VALUE = "yes-archive-pre-launch-bookings"


def archive_bookings_before(db: Session, cutoff: dt.date, *, actor: str) -> list[Booking]:
    """Core logic, independent of the CLI's env-var gating below so it's
    directly testable and reusable (e.g. from a one-off internal route).
    Returns the bookings actually archived, oldest event first."""
    target_ids = list(
        db.scalars(
            select(Booking.id).where(
                Booking.event_date.is_not(None),
                Booking.event_date <= cutoff,
                Booking.status != BookingStatus.archived,
            )
        ).all()
    )
    if not target_ids:
        return []

    bookings = db.scalars(
        select(Booking).where(Booking.id.in_(target_ids)).order_by(Booking.event_date, Booking.reference_code)
    ).all()

    archived = []
    for booking in bookings:
        change_status(
            db,
            booking,
            BookingStatus.archived,
            actor=actor,
            reason=f"Archived ahead of go-live -- pre-launch booking dated on or before {cutoff}",
        )
        archived.append(booking)
    return archived


def main() -> None:
    cutoff_raw = os.environ.get("ARCHIVE_CUTOFF_DATE")
    confirm = os.environ.get("ARCHIVE_CONFIRM")

    if not cutoff_raw:
        print("ARCHIVE_CUTOFF_DATE must be set (e.g. 2026-09-30). Aborting -- nothing was touched.")
        sys.exit(1)
    if confirm != REQUIRED_CONFIRM_VALUE:
        print(f"ARCHIVE_CONFIRM must be exactly '{REQUIRED_CONFIRM_VALUE}'. Aborting -- nothing was touched.")
        sys.exit(1)

    cutoff = dt.date.fromisoformat(cutoff_raw)
    db = SessionLocal()
    try:
        archived = archive_bookings_before(db, cutoff, actor="maintenance:archive_pre_launch")
        if not archived:
            print(f"No bookings found with event_date <= {cutoff} that aren't already archived. Nothing to do.")
            return
        print(f"Archived {len(archived)} bookings with event_date <= {cutoff}:")
        for b in archived:
            print(f"  {b.event_date} -- {b.reference_code} -- {b.event_name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
