"""Full-system pass: a single booking's journey across every phase --
enquiry -> booking -> BEO -> agreement -> signing -> invoice -> split
payment -> paid -- with the audit trail checked at the end. Each phase
has its own focused tests; this one exists to catch integration gaps
between them that no single phase's tests would ever see.
"""

import datetime as dt
from decimal import Decimal

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, BookingEvent, Contact
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.services.documents import create_new_version, mark_sent as mark_document_sent
from app.services.invoicing import create_invoice, get_payment_summary, mark_sent as mark_invoice_sent, record_payment


def test_full_journey_enquiry_to_paid_invoice_with_intact_audit_trail(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        # 1. Public enquiry (Phase 4) -- the only entry point a real client uses.
        # No space is picked here (matches the real public form): it lands in
        # the Unassigned/pending-triage space, same as an iVvy import.
        resp = client.post(
            "/enquiries",
            data={
                "first_name": "Taylor",
                "last_name": "Reeves",
                "email": "taylor.reeves@example.com",
                "phone": "0400111222",
                "event_name": "Reeves 40th Birthday",
                "event_date": "2027-02-13",
                "dates_flexible": "false",
                "attendee_count": 60,
                "adult_count": 60,
                # Deliberately a clean, unambiguous enquiry (real date, real
                # adult count, non-generic type, no accessibility mention) --
                # this test exercises the whole document/payment pipeline end
                # to end, not the enquiry-classification flags (see
                # test_enquiry_classification.py), so it must raise none.
                "event_type": "Group Lunch or Dinner",
                "proposed_time_slot": "Saturday evening",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        booking = db.query(Booking).filter_by(event_name="Reeves 40th Birthday").one()
        assert booking.status.value == "enquiry"
        assert booking.start_time is None  # Phase 1: unknown at enquiry stage, not guessed

        # Staff triages: assigns the real space and confirms with real times
        # (simulates the admin dashboard's assign-space + confirm flow --
        # direct service calls here, same as the rest of this build).
        booking.status = "confirmed"
        booking.space_id = loft.id
        booking.start_time = dt.time(18, 0)
        booking.end_time = dt.time(23, 0)
        db.add(booking)
        db.commit()
        db.refresh(booking)

        # 2. Availability now correctly reflects the confirmed booking (Phase 1)
        avail_resp = client.get(
            "/availability/spaces",
            params={"date": "2027-02-13", "start": "18:00:00", "end": "23:00:00", "guests": 60},
        )
        loft_result = next(s for s in avail_resp.json()["spaces"] if s["space_id"] == str(loft.id))
        assert loft_result["is_available"] is False
        assert "already_booked" in loft_result["reasons"]

        # 3. BEO + agreement generated from structured data alone (Phase 2)
        beo_content = generate_beo_content(
            booking,
            food_order_line_items=[{"item": "Canapes package", "quantity": 60, "unit_price": "22.00"}],
        )
        assert beo_content["total_food_spend"]["total"] == "1320.00"
        beo = create_new_version(db, booking, DocumentType.beo, beo_content, actor="staff")

        agreement = create_new_version(
            db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff"
        )
        agreement = mark_document_sent(db, agreement, actor="staff")

        # Client views and signs (Phase 2, over real HTTP)
        view_resp = client.get(f"/d/{agreement.access_token}")
        assert view_resp.status_code == 200
        assert "Reeves 40th Birthday" in view_resp.text

        sign_resp = client.post(f"/d/{agreement.access_token}/sign", data={"signer_name": "Taylor Reeves"})
        assert sign_resp.status_code in (200, 303)
        db.refresh(agreement)
        assert agreement.status == DocumentStatus.signed

        # 4. Final invoice, split across two payers (Phase 3)
        invoice = create_invoice(
            db,
            booking,
            InvoiceType.final,
            [{"description": "Canapes package", "quantity": 60, "unit_price": "22.00"}],
            due_date=dt.date(2027, 2, 1),
            actor="staff",
        )
        assert invoice.total == Decimal("1320.00")  # matches the BEO's own total -- no drift between them
        invoice = mark_invoice_sent(db, invoice, actor="staff")

        record_payment(
            db, invoice, amount=Decimal("660.00"), method=PaymentMethod.bank_transfer, payer_name="Taylor Reeves", actor="staff"
        )
        record_payment(
            db, invoice, amount=Decimal("660.00"), method=PaymentMethod.bank_transfer, payer_name="Reeves Family Trust", actor="staff"
        )

        summary = get_payment_summary(db, invoice)
        assert summary["is_fully_paid"] is True
        assert invoice.status == InvoiceStatus.paid

        # Client-facing invoice page reflects the paid-in-full state (over real HTTP)
        invoice_resp = client.get(f"/i/{invoice.access_token}")
        assert invoice_resp.status_code == 200
        assert "Paid in full" in invoice_resp.text
        assert "Taylor Reeves" in invoice_resp.text
        assert "Reeves Family Trust" in invoice_resp.text

        # 5. Audit trail reconstructs the whole story without re-reading any email
        events = db.query(BookingEvent).filter_by(booking_id=booking.id).order_by(BookingEvent.created_at).all()
        event_types = [e.event_type for e in events]
        assert event_types == [
            "created",
            "document_created",  # BEO
            "document_created",  # agreement
            "document_status_changed",  # agreement sent
            "document_status_changed",  # agreement viewed
            "document_status_changed",  # agreement signed
            "invoice_created",
            "invoice_status_changed",  # invoice sent
            "payment_received",
            "payment_received",
            "invoice_status_changed",  # invoice paid
        ]

        # Contact was created once and reused throughout, not duplicated
        contacts = db.query(Contact).filter_by(email="taylor.reeves@example.com").all()
        assert len(contacts) == 1
    finally:
        app.dependency_overrides.clear()
