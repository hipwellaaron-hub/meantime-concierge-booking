"""One-off cleanup: deletes the sample booking created by
create_sample_test_data.py, along with its documents, invoices,
payments, booking_events, and the sample contact (if it has no other
bookings). Safe to run more than once -- no-ops if nothing matches.

Not wired into any router. Intended to be run once via a temporary
preDeployCommand override, then removed.
"""

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Booking, BookingEvent, Contact, Document, Invoice, Payment

SAMPLE_EVENT_NAME = "SAMPLE TEST BOOKING (safe to delete)"


def main() -> None:
    db = SessionLocal()
    try:
        booking = db.execute(
            select(Booking).where(Booking.event_name == SAMPLE_EVENT_NAME)
        ).scalars().first()

        if booking is None:
            print("No sample booking found -- nothing to delete.")
            return

        contact_id = booking.contact_id

        invoice_ids = [
            row[0] for row in db.execute(select(Invoice.id).where(Invoice.booking_id == booking.id)).all()
        ]
        if invoice_ids:
            db.query(Payment).filter(Payment.invoice_id.in_(invoice_ids)).delete(synchronize_session=False)
        db.query(Invoice).filter(Invoice.booking_id == booking.id).delete(synchronize_session=False)
        db.query(Document).filter(Document.booking_id == booking.id).delete(synchronize_session=False)
        db.query(BookingEvent).filter(BookingEvent.booking_id == booking.id).delete(synchronize_session=False)
        db.delete(booking)
        db.commit()
        print(f"Deleted sample booking and its documents/invoices/payments/events.")

        if contact_id is not None:
            remaining = db.execute(
                select(Booking.id).where(Booking.contact_id == contact_id)
            ).first()
            if remaining is None:
                contact = db.get(Contact, contact_id)
                if contact is not None:
                    db.delete(contact)
                    db.commit()
                    print("Deleted sample contact (no other bookings referenced it).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
