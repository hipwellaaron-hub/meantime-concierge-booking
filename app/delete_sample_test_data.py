"""One-off cleanup for the sample booking created by
create_sample_test_data.py. Deletes its documents, invoices, and
payments (removing the live public links and any financial records).

booking_events is deliberately append-only at the database level (a
trigger rejects any DELETE), so the booking row and its audit trail
can't be removed -- that's the same immutable-audit-log design used
for every booking, not something worth carving out an exception for.
Instead the booking is marked cancelled and clearly relabeled, so it
reads unambiguously as inert test debris rather than a real booking.

Safe to run more than once -- no-ops if nothing matches.

Not wired into any router. Intended to be run once via a temporary
preDeployCommand override, then removed.
"""

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Booking, Document, Invoice, Payment
from app.models.booking import BookingStatus
from app.services.booking import change_status

SAMPLE_EVENT_NAME = "SAMPLE TEST BOOKING (safe to delete)"
CANCELLED_EVENT_NAME = "SAMPLE TEST BOOKING (cancelled, kept for immutable audit trail)"


def main() -> None:
    db = SessionLocal()
    try:
        booking = db.execute(
            select(Booking).where(Booking.event_name == SAMPLE_EVENT_NAME)
        ).scalars().first()

        if booking is None:
            print("No sample booking found -- nothing to delete.")
            return

        invoice_ids = [
            row[0] for row in db.execute(select(Invoice.id).where(Invoice.booking_id == booking.id)).all()
        ]
        if invoice_ids:
            db.query(Payment).filter(Payment.invoice_id.in_(invoice_ids)).delete(synchronize_session=False)
        db.query(Invoice).filter(Invoice.booking_id == booking.id).delete(synchronize_session=False)
        db.query(Document).filter(Document.booking_id == booking.id).delete(synchronize_session=False)
        db.commit()
        print("Deleted sample documents, invoices, and payments (public links are now dead).")

        db.refresh(booking)
        if booking.status != BookingStatus.cancelled:
            change_status(db, booking, BookingStatus.cancelled, actor="admin:sample_data_script")
        booking.event_name = CANCELLED_EVENT_NAME
        db.commit()
        print(
            f"Booking {booking.reference_code} marked cancelled and relabeled. "
            "Its row and audit trail remain (booking_events is append-only by design)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
