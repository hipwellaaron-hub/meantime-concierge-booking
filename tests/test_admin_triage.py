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


def test_triage_lists_enquiries_needing_clarification(admin_client, db, unassigned_space):
    from app.services.enquiry_classification import classify_and_flag

    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Generic Birthday Enquiry", event_type="Birthday", adult_count=40, child_count=0,
        notes=None, actor="test",
    )
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Generic Birthday Enquiry" in resp.text
    assert "milestone and guest ages not yet known" in resp.text


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


def test_triage_lists_flagged_bookings_in_progress(admin_client, db, unassigned_space):
    from app.services.enquiry_classification import classify_and_flag

    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Progressed Flagged Booking", event_type="Birthday", adult_count=40, child_count=0,
        notes=None, actor="test",
    )
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")
    change_status(db, booking, BookingStatus.offered, actor="test")

    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Progressed Flagged Booking" in resp.text
    # It has moved off the enquiry-stage section...
    assert resp.text.index("Flagged bookings still open") < resp.text.index("Progressed Flagged Booking")


def test_triage_does_not_list_flagged_booking_still_at_enquiry_in_the_in_progress_section(
    admin_client, db, unassigned_space
):
    from app.services.enquiry_classification import classify_and_flag

    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Still At Enquiry", event_type="Birthday", adult_count=40, child_count=0,
        notes=None, actor="test",
    )
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    resp = admin_client.get("/admin/triage")
    in_progress_section = resp.text.split("Flagged bookings still open")[1].split("Awaiting space")[0]
    assert "Still At Enquiry" not in in_progress_section


def test_triage_lists_unrecorded_minimum_reduction(admin_client, db, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Silently Reduced Minimum", event_type=None, adult_count=20, child_count=0,
        notes=None, actor="test", agreed_min_adults=loft.standard_min_adults - 10,
    )

    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Silently Reduced Minimum" in resp.text
    assert "Agreed minimum differs from standard" in resp.text


def test_triage_does_not_list_recorded_minimum_reduction(admin_client, db, loft):
    from app.models.booking import MinReductionReasonCode
    from app.services.booking import set_agreed_minimum

    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        event_name="Properly Recorded Reduction", event_type=None, adult_count=20, child_count=0,
        notes=None, actor="test",
    )
    set_agreed_minimum(
        db, booking, agreed_min_adults=loft.standard_min_adults - 10,
        reason=MinReductionReasonCode.returning_client, actor="test",
    )

    resp = admin_client.get("/admin/triage")
    assert "Properly Recorded Reduction" not in resp.text


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
