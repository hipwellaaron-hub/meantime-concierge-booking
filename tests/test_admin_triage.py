import datetime as dt
import re

from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import change_status, create_booking


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_triage_lists_unassigned_bookings(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Imported Booking", event_type=None, adult_count=20, child_count=0,
        notes=None, actor="test", status=BookingStatus.confirmed,
    )

    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Imported Booking" in resp.text


def test_triage_assign_space_moves_booking_out_of_worklist(admin_client, db, unassigned_space, loft):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Imported Booking", event_type=None, adult_count=20, child_count=0,
        notes=None, actor="test", status=BookingStatus.confirmed,
    )

    page = admin_client.get("/admin/triage")
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={"csrf_token": csrf_token, "space_id": str(loft.id), "start_time": "18:00", "end_time": "23:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(booking)
    assert booking.space_id == loft.id
    assert str(booking.start_time) == "18:00:00"

    after = admin_client.get("/admin/triage")
    assert "Imported Booking" not in after.text


def test_triage_lists_wizard_ready_bookings(admin_client, db, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date.today() + dt.timedelta(days=3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Wizard Ready Booking",
        event_type=None, adult_count=20, child_count=0, notes=None, actor="test", status=BookingStatus.confirmed,
    )
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=500, method=PaymentMethod.card, actor="test")
    agreement = documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    agreement = documents_service.mark_sent(db, agreement, actor="test")
    documents_service.sign(db, agreement, signer_name="Test Client", signer_ip="127.0.0.1")

    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Wizard Ready Booking" in resp.text


def test_triage_send_wizard_link_creates_session(admin_client, db, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date.today() + dt.timedelta(days=3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Wizard Ready Booking",
        event_type=None, adult_count=20, child_count=0, notes=None, actor="test", status=BookingStatus.confirmed,
    )
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=500, method=PaymentMethod.card, actor="test")
    agreement = documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    agreement = documents_service.mark_sent(db, agreement, actor="test")
    documents_service.sign(db, agreement, signer_name="Test Client", signer_ip="127.0.0.1")

    page = admin_client.get("/admin/triage")
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session is not None

    # get_wizard_eligible_bookings only excludes a *submitted* wizard (see
    # app/services/wizard.py) -- a merely-sent, not-yet-submitted session
    # deliberately keeps the booking on the worklist, so staff can still
    # see and re-send the link. Posting /wizard/send again must stay
    # idempotent (no duplicate session) rather than erroring.
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp2.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session.id is not None
