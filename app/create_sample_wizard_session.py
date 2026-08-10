"""One-off script: creates a sample booking that's fully eligible for the
Guided Booking Wizard (deposit paid + agreement signed), and a wizard
session for it, so Aaron (or whoever he shares the link with) can click
through the real wizard UI on production. Idempotent per label -- safe to
run more than once with the same label, prints the existing link instead
of duplicating.

Pass a label as the first CLI arg to create multiple independent test
bookings/sessions (each gets its own event name, contact, and wizard
token) -- e.g. `python -m app.create_sample_wizard_session B` for a
second, independent tester link.

Not wired into any router or scheduled job. Intended to be run once via a
temporary preDeployCommand override, then removed.
"""

import datetime as dt
import sys
import uuid

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Booking, Invoice
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking
from app.services.contact_matching import find_or_create_contact

SAMPLE_SPACE_ID = uuid.UUID("f8d85609-01f4-4375-9629-86b8dd5c3fd4")  # The Loft, Hamilton
BASE_URL = "https://meantime-concierge-booking-production.up.railway.app"


def main(label: str = "") -> None:
    suffix = f" {label}" if label else ""
    sample_email = f"sample-wizard-test{('-' + label.lower()) if label else ''}@meantime-concierge.internal"
    sample_event_name = f"SAMPLE WIZARD TEST BOOKING{suffix} (safe to delete)"

    db = SessionLocal()
    try:
        booking = db.execute(
            select(Booking).where(Booking.event_name == sample_event_name)
        ).scalars().first()

        if booking is None:
            # Each label gets its own date -- these are all real 'confirmed'
            # bookings on the same real space, so the double-booking
            # exclusion constraint (correctly) rejects overlapping ones.
            date_offset = (ord(label[0]) - ord("A")) if label else 0
            event_date = dt.date(2027, 3, 20) + dt.timedelta(days=date_offset)
            contact, _ = find_or_create_contact(db, f"Sample Wizard Tester{suffix}", sample_email, None)
            booking = create_booking(
                db,
                space_id=SAMPLE_SPACE_ID,
                contact_id=contact.id,
                event_date=event_date,
                start_time=dt.time(18, 0),
                end_time=dt.time(23, 0),
                event_name=sample_event_name,
                event_type="Sample/Test",
                adult_count=60,
                child_count=4,
                notes="Created for testing the live Guided Booking Wizard. Safe to delete.",
                actor="admin:sample_wizard_script",
                status=BookingStatus.confirmed,
            )
            booking.outside_cake_permitted = True  # exercise the grandfathered-cake path too
            db.commit()
        print(f"SAMPLE_BOOKING_REF{suffix}: {booking.reference_code}")

        deposit_invoice = db.execute(
            select(Invoice).where(Invoice.booking_id == booking.id, Invoice.type == InvoiceType.deposit)
        ).scalars().first()
        if deposit_invoice is None:
            deposit_invoice = invoicing.create_invoice(
                db, booking, InvoiceType.deposit,
                [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
                dt.date.today(), actor="admin:sample_wizard_script",
            )
        if deposit_invoice.status.value == "draft":
            deposit_invoice = invoicing.mark_sent(db, deposit_invoice, actor="admin:sample_wizard_script")
        if invoicing.get_total_paid(db, deposit_invoice.id) <= 0:
            invoicing.record_payment(
                db, deposit_invoice, amount=500, method=PaymentMethod.card, actor="admin:sample_wizard_script"
            )

        agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)
        if agreement is None:
            agreement = documents_service.create_new_version(
                db, booking, DocumentType.agreement, {"terms": "sample test agreement"}, actor="admin:sample_wizard_script"
            )
        if agreement.status.value == "draft":
            agreement = documents_service.mark_sent(db, agreement, actor="admin:sample_wizard_script")
        if agreement.status.value in ("sent", "viewed"):
            agreement = documents_service.sign(
                db, agreement, signer_name="Sample Wizard Tester", signer_ip="0.0.0.0"
            )

        session = wizard_service.get_or_create_session(db, booking, actor="admin:sample_wizard_script")
        print(f"SAMPLE_WIZARD_URL{suffix}: {BASE_URL}/w/{session.access_token}")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
