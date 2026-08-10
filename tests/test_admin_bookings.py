import datetime as dt
import re

from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import change_status, create_booking


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _detail_page(admin_client, booking_id):
    return admin_client.get(f"/admin/bookings/{booking_id}")


def test_bookings_list_shows_booking(admin_client, booking):
    resp = admin_client.get("/admin/bookings")
    assert resp.status_code == 200
    assert "Wilson Wedding" in resp.text
    assert booking.reference_code in resp.text


def test_bookings_list_search_filters(admin_client, booking):
    resp = admin_client.get("/admin/bookings", params={"q": "does-not-exist"})
    assert "Wilson Wedding" not in resp.text

    resp = admin_client.get("/admin/bookings", params={"q": "Wilson"})
    assert "Wilson Wedding" in resp.text


def test_booking_detail_renders(admin_client, booking):
    resp = _detail_page(admin_client, booking.id)
    assert resp.status_code == 200
    assert "Wilson Wedding" in resp.text


def test_generate_and_send_beo(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert beo is not None
    assert beo.status == DocumentStatus.draft

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{beo.id}/send",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(beo)
    assert beo.status == DocumentStatus.sent


def test_create_send_and_pay_deposit_invoice(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/deposit",
        data={"csrf_token": csrf_token, "due_date": "2026-09-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    invoice = booking.invoices[0]
    assert invoice.status == InvoiceStatus.draft

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/send",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent

    page3 = _detail_page(admin_client, booking.id)
    csrf_token3 = _csrf(page3.text)
    resp3 = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/payments",
        data={
            "csrf_token": csrf_token3,
            "amount": str(invoice.total),
            "method": "bank_transfer",
            "reference": "TXN123",
            "payer_name": "Pat Wilson",
        },
        follow_redirects=False,
    )
    assert resp3.status_code == 303
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_send_and_revoke_wizard_link(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session is not None
    token = booking.wizard_session.access_token
    assert token in _detail_page(admin_client, booking.id).text

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/revoke", data={"csrf_token": csrf_token2}, follow_redirects=False
    )
    assert resp2.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session.status.value == "revoked"


def test_confirm_early_setup_access(admin_client, db, booking):
    booking.setup_access_time = dt.time(11, 0)
    booking.setup_access_confirmed = False
    db.commit()

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.setup_access_confirmed is True


def test_confirm_early_setup_access_with_no_pending_request_errors(admin_client, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_set_agreed_minimum_requires_reason_when_reduced(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={"csrf_token": csrf_token, "agreed_min_adults": str(booking.space.standard_min_adults - 5)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.agreed_min_adults == booking.space.standard_min_adults


def test_set_agreed_minimum_with_reason_succeeds(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    reduced = booking.space.standard_min_adults - 5
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={
            "csrf_token": csrf_token,
            "agreed_min_adults": str(reduced),
            "reason": MinReductionReasonCode.returning_client.value,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == reduced
    assert booking.agreed_min_reduction_reason == MinReductionReasonCode.returning_client


def test_toggle_outside_cake_permitted(admin_client, db, booking):
    assert booking.outside_cake_permitted is False

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake",
        data={"csrf_token": csrf_token, "permitted": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.outside_cake_permitted is True

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake",
        data={"csrf_token": csrf_token2, "permitted": "false"},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(booking)
    assert booking.outside_cake_permitted is False


def test_assign_space_conflict_returns_409(admin_client, db, loft, unassigned_space):
    existing = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 8, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Existing Confirmed",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    change_status(db, existing, BookingStatus.confirmed, actor="test")

    conflicting = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 8, 1),
        event_name="Needs Triage", event_type=None, adult_count=10, child_count=0,
        notes=None, actor="test", status=BookingStatus.confirmed,
    )

    page = _detail_page(admin_client, conflicting.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{conflicting.id}/assign-space",
        data={"csrf_token": csrf_token, "space_id": str(loft.id), "start_time": "18:00", "end_time": "23:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_assign_space_can_set_a_previously_missing_event_date(admin_client, db, loft, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="No Date Yet", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.event_date == dt.date(2027, 9, 4)


def test_assign_space_with_blank_event_date_leaves_it_unset(admin_client, db, loft, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="Still No Date", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={"csrf_token": csrf_token, "space_id": str(loft.id), "start_time": "18:00", "end_time": "23:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.event_date is None


def test_assign_space_reconciles_untouched_agreed_minimum_to_new_space_standard(
    admin_client, db, loft, unassigned_space
):
    """A booking created in the Unassigned placeholder (standard_min_adults
    always 0) must pick up the real space's standard minimum on assignment,
    not carry the stale 0 forward with no reason recorded."""
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="From Unassigned", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )
    assert booking.agreed_min_adults == unassigned_space.standard_min_adults

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == loft.standard_min_adults
    assert booking.agreed_min_reduction_reason is None


def test_assign_space_leaves_a_deliberately_set_agreed_minimum_alone(admin_client, db, loft, unassigned_space):
    from app.models.booking import MinReductionReasonCode
    from app.services.booking import set_agreed_minimum

    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="Custom Minimum Already Set", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )
    set_agreed_minimum(
        db, booking, agreed_min_adults=5, reason=MinReductionReasonCode.aaron_discretion, actor="test"
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == 5
    assert booking.agreed_min_reduction_reason == MinReductionReasonCode.aaron_discretion


def test_transition_status_via_dashboard_succeeds(admin_client, db, booking):
    assert booking.status == BookingStatus.enquiry
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/status",
        data={"csrf_token": csrf_token, "new_status": "offered", "reason": "quoted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.status == BookingStatus.offered


def test_transition_status_via_dashboard_rejects_illegal_move(admin_client, db, booking):
    assert booking.status == BookingStatus.enquiry
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/status",
        data={"csrf_token": csrf_token, "new_status": "confirmed"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry


def test_booking_detail_shows_only_legal_next_statuses(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    assert 'value="offered"' in page.text
    assert 'value="dead"' in page.text
    assert 'value="confirmed"' not in page.text
    assert 'value="completed"' not in page.text


def test_sending_wizard_link_without_event_date_is_rejected(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="No Date Yet", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.wizard_session is None
