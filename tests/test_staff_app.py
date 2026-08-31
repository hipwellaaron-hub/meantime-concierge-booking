"""Meantime Floor: the staff app's auth, its read-only API, and the admin
staff-management page. The load-bearing test here is
test_beo_view_does_not_mark_document_viewed -- the whole reason the app
renders the BEO itself instead of reusing the public /d/{token} link.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import StaffAppToken
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing, staff_auth
from app.services.booking import add_linked_space, change_status, create_booking
from tests.conftest import STAFF_TEST_PASSWORD

FLOOR_EMAIL = "floor@meantime.com.au"
FLOOR_PASSWORD = "floorpassword1"


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def floor_user(db):
    return staff_auth.create_or_update_staff_user(
        db, email=FLOOR_EMAIL, name="Casual Floor", password=FLOOR_PASSWORD, role="floor"
    )


def _login(client, email=FLOOR_EMAIL, password=FLOOR_PASSWORD) -> dict:
    resp = client.post("/api/staff/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _confirmed_booking(db, space, contact, *, event_date=dt.date(2026, 10, 3), event_name="Floor Test", **overrides):
    booking = create_booking(
        db,
        space_id=space.id,
        contact_id=contact.id,
        event_date=event_date,
        start_time=overrides.pop("start_time", dt.time(18, 0)),
        end_time=overrides.pop("end_time", dt.time(23, 0)),
        event_name=event_name,
        event_type=overrides.pop("event_type", "birthday"),
        adult_count=overrides.pop("adult_count", 40),
        child_count=overrides.pop("child_count", 0),
        notes=None,
        actor="test",
        **overrides,
    )
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    return booking


# ---------------------------------------------------------------- login/auth


def test_login_returns_working_token(client, db, floor_user):
    headers = _login(client)
    resp = client.get("/api/staff/bookings", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"bookings": []}


def test_login_wrong_password_401(client, db, floor_user):
    resp = client.post("/api/staff/login", json={"email": FLOOR_EMAIL, "password": "not-the-password"})
    assert resp.status_code == 401


def test_login_inactive_account_401(client, db, floor_user):
    floor_user.is_active = False
    db.commit()
    resp = client.post("/api/staff/login", json={"email": FLOOR_EMAIL, "password": FLOOR_PASSWORD})
    assert resp.status_code == 401


def test_missing_or_garbage_token_401(client, db, floor_user):
    assert client.get("/api/staff/bookings").status_code == 401
    assert client.get("/api/staff/bookings", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_revoked_token_401(client, db, floor_user):
    headers = _login(client)
    token = db.query(StaffAppToken).filter_by(staff_user_id=floor_user.id).one()
    staff_auth.revoke_app_token(db, token.id)
    assert client.get("/api/staff/bookings", headers=headers).status_code == 401


def test_deactivating_user_kills_live_tokens(client, db, floor_user):
    headers = _login(client)
    floor_user.is_active = False
    db.commit()
    assert client.get("/api/staff/bookings", headers=headers).status_code == 401


def test_floor_role_cannot_enter_admin(client, db, floor_user):
    """A floor login must bounce off /admin even with valid credentials --
    require_staff rejects role='floor' outright."""
    import re

    login_page = client.get("/admin/login")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/admin/login",
        data={"csrf_token": csrf_token, "email": FLOOR_EMAIL, "password": FLOOR_PASSWORD, "next": "/admin/"},
    )
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/admin/login" in resp.headers["location"]


def test_admin_role_token_also_works_on_app_api(client, db, staff_user):
    headers = _login(client, email=staff_user.email, password=STAFF_TEST_PASSWORD)
    assert client.get("/api/staff/bookings", headers=headers).status_code == 200


def test_invalid_role_rejected(db):
    with pytest.raises(ValueError):
        staff_auth.create_or_update_staff_user(
            db, email="x@meantime.com.au", name="X", password="longenough1", role="superuser"
        )


# ---------------------------------------------------------------- listings


def test_only_confirmed_and_completed_listed(client, db, loft, mezzanine, lounge, contact, floor_user):
    headers = _login(client)
    visible_confirmed = _confirmed_booking(db, loft, contact, event_name="Confirmed One")
    visible_completed = _confirmed_booking(
        db, mezzanine, contact, event_date=dt.date(2026, 10, 10), event_name="Completed One"
    )
    change_status(db, visible_completed, BookingStatus.completed, actor="test")
    for status, day in [
        (BookingStatus.enquiry, 17),
        (BookingStatus.tentative, 18),
        (BookingStatus.cancelled, 24),
        (BookingStatus.archived, 25),
    ]:
        b = create_booking(
            db,
            space_id=lounge.id,
            contact_id=contact.id,
            event_date=dt.date(2026, 10, day),
            start_time=dt.time(18, 0),
            end_time=dt.time(22, 0),
            event_name=f"Hidden {status.value}",
            event_type="birthday",
            adult_count=20,
            child_count=0,
            notes=None,
            actor="test",
        )
        change_status(db, b, status, actor="test")

    names = [b["event_name"] for b in client.get("/api/staff/bookings", headers=headers).json()["bookings"]]
    assert names == ["Confirmed One", "Completed One"]
    assert str(visible_confirmed.id) in [
        b["id"] for b in client.get("/api/staff/bookings", headers=headers).json()["bookings"]
    ]


def test_date_range_filters(client, db, loft, mezzanine, contact, floor_user):
    headers = _login(client)
    _confirmed_booking(db, loft, contact, event_date=dt.date(2026, 10, 3), event_name="October")
    _confirmed_booking(db, mezzanine, contact, event_date=dt.date(2026, 11, 7), event_name="November")

    october = client.get(
        "/api/staff/bookings", params={"from": "2026-10-01", "to": "2026-10-31"}, headers=headers
    ).json()["bookings"]
    assert [b["event_name"] for b in october] == ["October"]


def test_linked_child_folds_into_parent(client, db, loft, mezzanine, contact, floor_user):
    headers = _login(client)
    parent = _confirmed_booking(db, loft, contact, event_name="Two Rooms")
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")

    bookings = client.get("/api/staff/bookings", headers=headers).json()["bookings"]
    assert len(bookings) == 1  # the child is never its own row
    assert bookings[0]["space"] == "The Loft"
    assert sorted(bookings[0]["spaces"]) == ["The Loft", "The Mezzanine"]

    # And the child 404s if addressed directly.
    assert client.get(f"/api/staff/bookings/{child.id}", headers=headers).status_code == 404


def test_detail_includes_run_order_times(client, db, loft, contact, floor_user):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    booking.setup_access_time = dt.time(15, 0)
    booking.setup_access_confirmed = False
    booking.food_service_time = dt.time(19, 30)
    db.commit()

    detail = client.get(f"/api/staff/bookings/{booking.id}", headers=headers).json()
    assert detail["setup_access_time"] == "15:00"
    assert detail["setup_access_confirmed"] is False
    assert detail["food_service_time"] == "19:30"
    assert detail["adults"] == 40


# ---------------------------------------------------------------- payment status


def _deposit(db, booking, *, paid: bool):
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoicing.mark_sent(db, invoice, actor="test")
    if paid:
        invoicing.record_payment(
            db, invoice, amount=invoice.total, method=PaymentMethod.bank_transfer, actor="test"
        )
    return invoice


def _payment_of(client, headers, booking) -> str:
    return client.get(f"/api/staff/bookings/{booking.id}", headers=headers).json()["payment_status"]


def test_payment_status_matrix(client, db, loft, mezzanine, lounge, contact, floor_user):
    headers = _login(client)

    # No invoices at all -> outstanding (the honest weekend answer).
    no_invoices = _confirmed_booking(db, loft, contact, event_date=dt.date(2026, 10, 3))
    assert _payment_of(client, headers, no_invoices) == "outstanding"

    # Sent, unpaid -> outstanding.
    unpaid = _confirmed_booking(db, mezzanine, contact, event_date=dt.date(2026, 10, 10))
    _deposit(db, unpaid, paid=False)
    assert _payment_of(client, headers, unpaid) == "outstanding"

    # Deposit paid but final still sent -> outstanding.
    partial = _confirmed_booking(db, lounge, contact, event_date=dt.date(2026, 10, 17))
    _deposit(db, partial, paid=True)
    final = invoicing.create_final_invoice(
        db,
        partial,
        line_items=[{"description": "Catering", "quantity": 1, "unit_price": "2000.00"}],
        due_date=dt.date(2026, 10, 10),
        actor="test",
    )
    invoicing.mark_sent(db, final, actor="test")
    assert _payment_of(client, headers, partial) == "outstanding"

    # Everything paid -> paid.
    invoicing.record_payment(db, final, amount=final.total, method=PaymentMethod.bank_transfer, actor="test")
    assert _payment_of(client, headers, partial) == "paid"

    # A cancelled invoice is ignored: paid deposit + cancelled final -> paid.
    cancelled_case = _confirmed_booking(db, loft, contact, event_date=dt.date(2026, 10, 24))
    _deposit(db, cancelled_case, paid=True)
    dud = invoicing.create_final_invoice(
        db,
        cancelled_case,
        line_items=[{"description": "Catering", "quantity": 1, "unit_price": "2000.00"}],
        due_date=dt.date(2026, 10, 20),
        actor="test",
    )
    invoicing.cancel_invoice(db, dud, actor="test")
    assert _payment_of(client, headers, cancelled_case) == "paid"


# ---------------------------------------------------------------- BEO


def _beo_content(booking):
    """Real generated content (the template needs its full _reference
    structure) with an internal note layered on top."""
    from app.services.document_generation import generate_beo_content

    content = generate_beo_content(booking)
    content["internal_notes"] = "KITCHEN: fire pizzas at 7pm sharp"
    return content


def test_beo_ready_flips_when_beo_leaves_draft(client, db, loft, contact, floor_user):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")

    assert _payment_of(client, headers, booking) is not None  # detail reachable
    assert client.get(f"/api/staff/bookings/{booking.id}", headers=headers).json()["beo_ready"] is False
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 404

    documents_service.mark_sent(db, document, actor="test")
    assert client.get(f"/api/staff/bookings/{booking.id}", headers=headers).json()["beo_ready"] is True
    resp = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers)
    assert resp.status_code == 200
    assert "fire pizzas at 7pm sharp" in resp.text  # internal notes ARE the floor view


def test_beo_view_does_not_mark_document_viewed(client, db, loft, contact, floor_user):
    """THE reason the app has its own BEO route: 'viewed' must keep
    meaning the CLIENT opened their link, not that a bartender did."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    document = documents_service.mark_sent(db, document, actor="test")

    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 200
    db.refresh(document)
    assert document.viewed_at is None
    assert document.status == DocumentStatus.sent


def test_beo_pdf_is_clean_client_render(client, db, loft, contact, floor_user):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, document, actor="test")

    resp = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers)
    assert "fire pizzas" in resp.text

    pdf = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"
    assert pdf.headers["content-disposition"].endswith('-BEO-v1.pdf"')
    # The PDF must NOT carry the kitchen notes -- a downloaded copy can
    # leave the team. Extract its text to prove it.
    from io import BytesIO

    from pypdf import PdfReader

    text = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "fire pizzas" not in text


def test_beo_of_hidden_booking_404(client, db, loft, contact, floor_user):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, document, actor="test")
    change_status(db, booking, BookingStatus.cancelled, actor="test")
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 404


# ---------------------------------------------------------------- admin page


def _csrf_of(admin_client, path="/admin/staff") -> str:
    import re

    return re.search(r'name="csrf_token" value="([^"]+)"', admin_client.get(path).text).group(1)


def test_admin_creates_floor_account_that_can_log_in(db, admin_client):
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf,
            "name": "New Floor",
            "email": "newfloor@meantime.com.au",
            "password": "brandnewpass1",
            "role": "floor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    login = admin_client.post(
        "/api/staff/login", json={"email": "newfloor@meantime.com.au", "password": "brandnewpass1"}
    )
    assert login.status_code == 200


def test_admin_cannot_deactivate_self(db, admin_client, staff_user):
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        f"/admin/staff/{staff_user.id}/deactivate", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 422


def test_admin_deactivate_and_reactivate(db, admin_client, floor_user):
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        f"/admin/staff/{floor_user.id}/deactivate", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.refresh(floor_user)
    assert floor_user.is_active is False

    resp = admin_client.post(
        f"/admin/staff/{floor_user.id}/reactivate", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.refresh(floor_user)
    assert floor_user.is_active is True


def test_admin_revokes_single_token(db, admin_client, floor_user):
    raw_a = staff_auth.issue_app_token(db, floor_user)
    raw_b = staff_auth.issue_app_token(db, floor_user)
    token_a = next(
        t
        for t in db.query(StaffAppToken).filter_by(staff_user_id=floor_user.id)
        if t.token_hash == staff_auth._hash_token(raw_a)
    )
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        f"/admin/staff/tokens/{token_a.id}/revoke", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert staff_auth.get_staff_by_app_token(db, raw_a) is None
    assert staff_auth.get_staff_by_app_token(db, raw_b) is not None  # only that token died


# ---------------------------------------------------------------- shell routes


def test_floor_shell_and_pwa_assets(client):
    shell = client.get("/floor")
    assert shell.status_code == 200
    assert "Meantime Floor" in shell.text

    manifest = client.get("/floor/manifest.json")
    assert manifest.status_code == 200
    assert manifest.json()["name"] == "Meantime Floor"
    assert manifest.json()["display"] == "standalone"

    sw = client.get("/floor/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/floor"


# ---------------------------------------------------------------- floor welcome email


def test_creating_floor_account_sends_welcome_email(db, admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.notifications.notify_floor_welcome",
        lambda **kw: calls.append(kw) or True,
    )
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf,
            "name": "Sally Hipwell",
            "email": "sally.welcome@meantime.com.au",
            "password": "sallypass12",
            "role": "floor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "welcome=sent" in resp.headers["location"]
    assert len(calls) == 1
    assert calls[0]["email"] == "sally.welcome@meantime.com.au"


def test_creating_admin_account_sends_no_welcome_email(db, admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.notifications.notify_floor_welcome", lambda **kw: calls.append(kw) or True)
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf,
            "name": "New Admin",
            "email": "new.admin@meantime.com.au",
            "password": "adminpass12",
            "role": "admin",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert calls == []  # admins use the dashboard, not /floor


def test_floor_welcome_failure_still_creates_account(db, admin_client, monkeypatch):
    monkeypatch.setattr("app.services.notifications.notify_floor_welcome", lambda **kw: False)
    csrf = _csrf_of(admin_client)
    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf,
            "name": "Karly Floor",
            "email": "karly.floor@meantime.com.au",
            "password": "karlypass12",
            "role": "floor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "welcome=failed" in resp.headers["location"]
    # account exists regardless
    assert staff_auth.authenticate(db, "karly.floor@meantime.com.au", "karlypass12") is not None
