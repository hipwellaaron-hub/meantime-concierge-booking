"""One-off script: creates a single sample booking with a BEO, an
agreement, and a deposit invoice, all marked 'sent' so their public links
work immediately. For Aaron to click through the real signing/payment
flow against production. Idempotent -- safe to run more than once, and
prints the existing links instead of duplicating on a second run.

Not wired into any router or scheduled job. Intended to be run once via a
temporary preDeployCommand override, then removed.
"""

import datetime as dt
import uuid

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Booking, Invoice
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.services.booking import create_booking
from app.services.contact_matching import find_or_create_contact
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.services.documents import create_new_version, get_current
from app.services.documents import mark_sent as mark_document_sent
from app.services.invoicing import create_deposit_invoice
from app.services.invoicing import mark_sent as mark_invoice_sent

SAMPLE_EMAIL = "sample-test@meantime-concierge.internal"
SAMPLE_EVENT_NAME = "SAMPLE TEST BOOKING (safe to delete)"
SAMPLE_EVENT_DATE = dt.date(2026, 9, 15)
SAMPLE_SPACE_ID = uuid.UUID("f8d85609-01f4-4375-9629-86b8dd5c3fd4")  # The Loft, Hamilton
BASE_URL = "https://meantime-concierge-booking-production.up.railway.app"


def main() -> None:
    db = SessionLocal()
    try:
        booking = db.execute(
            select(Booking).where(Booking.event_name == SAMPLE_EVENT_NAME)
        ).scalars().first()

        if booking is None:
            contact, _ = find_or_create_contact(db, "Sample Test Person", SAMPLE_EMAIL, None)
            booking = create_booking(
                db,
                space_id=SAMPLE_SPACE_ID,
                contact_id=contact.id,
                event_date=SAMPLE_EVENT_DATE,
                start_time=dt.time(18, 0),
                end_time=dt.time(23, 0),
                event_name=SAMPLE_EVENT_NAME,
                event_type="Sample/Test",
                adult_count=50,
                child_count=0,
                notes="Created for testing the live document/invoice/signing/payment flow. Safe to delete.",
                actor="admin:sample_data_script",
                status=BookingStatus.confirmed,
            )
        print(f"SAMPLE_BOOKING_REF: {booking.reference_code}")

        beo = get_current(db, booking.id, DocumentType.beo)
        if beo is None:
            beo = create_new_version(db, booking, DocumentType.beo, generate_beo_content(booking), actor="admin:sample_data_script")
        if beo.status.value == "draft":
            beo = mark_document_sent(db, beo, actor="admin:sample_data_script")
        print(f"SAMPLE_BEO_URL: {BASE_URL}/d/{beo.access_token}")

        agreement = get_current(db, booking.id, DocumentType.agreement)
        if agreement is None:
            agreement = create_new_version(
                db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="admin:sample_data_script"
            )
        if agreement.status.value == "draft":
            agreement = mark_document_sent(db, agreement, actor="admin:sample_data_script")
        print(f"SAMPLE_AGREEMENT_URL: {BASE_URL}/d/{agreement.access_token}")

        invoice = db.execute(
            select(Invoice).where(Invoice.booking_id == booking.id, Invoice.type == InvoiceType.deposit)
        ).scalars().first()
        if invoice is None:
            invoice = create_deposit_invoice(
                db, booking, due_date=dt.date.today() + dt.timedelta(days=14), actor="admin:sample_data_script"
            )
        if invoice.status.value == "draft":
            invoice = mark_invoice_sent(db, invoice, actor="admin:sample_data_script")
        print(f"SAMPLE_INVOICE_URL: {BASE_URL}/i/{invoice.access_token}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
