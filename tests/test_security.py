"""Adversarial regression tests for the security review of Aug 2026.

Each test pins a fix so a future change can't silently reintroduce the
hole. Grouped by the finding it guards.
"""

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _booking(db, space, *, event_name="Security Test"):
    contact = Contact(name="sec test", email="sec.test@example.com", phone="0400111222")
    db.add(contact)
    db.flush()
    return create_booking(
        db,
        space_id=space.id,
        contact_id=contact.id,
        event_date=dt.date(2027, 3, 6),
        start_time=dt.time(18, 0),
        end_time=dt.time(23, 0),
        event_name=event_name,
        event_type="birthday",
        adult_count=50,
        child_count=0,
        notes=None,
        actor="test",
    )


# ---------------------------------------------------------------- XSS (F1 injection)


def test_wizard_bootstrap_escapes_script_break_out(client, db, loft):
    """A client-supplied event name containing </script> must not break out
    of the JSON island in the wizard page. |tojson escapes it to \\u003c."""
    booking = _booking(db, loft, event_name="</script><img src=x onerror=alert(1)>")
    session = wizard_service.get_or_create_session(db, booking, actor="test")

    resp = client.get(f"/w/{session.access_token}")
    assert resp.status_code == 200
    # The dangerous literal must be absent; its escaped form must be present
    # inside the bootstrap-data island.
    assert "</script><img" not in resp.text
    assert "\\u003c/script\\u003e" in resp.text or "\\u003cimg" in resp.text


# ---------------------------------------------------------------- Floor money leak (F2 auth)


def _sent_beo(db, booking):
    from app.services.document_generation import generate_beo_content

    content = generate_beo_content(booking)
    content["internal_notes"] = "KITCHEN: internal only"
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    return documents_service.mark_sent(db, doc, actor="test")


def test_floor_beo_html_hides_money_and_phone(client, db, loft):
    """The floor BEO view (is_floor_app) shows kitchen notes but must NOT
    show dollar figures or the client's phone number."""
    from app.services import staff_auth

    booking = _booking(db, loft)
    booking.contact.phone = "0400999888"
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _sent_beo(db, booking)

    staff = staff_auth.create_or_update_staff_user(
        db, email="floorsec@meantime.com.au", name="Floor Sec", password="floorpass12", role="floor"
    )
    token = staff_auth.issue_app_token(db, staff)
    html = client.get(
        f"/api/staff/bookings/{booking.id}/beo", headers={"Authorization": f"Bearer {token}"}
    ).text

    assert "internal only" in html  # the floor view DOES show kitchen notes
    assert "Billing Summary" not in html
    assert "Balance owing" not in html
    assert "0400999888" not in html  # client phone withheld


# ---------------------------------------------------------------- X-Forwarded-For (F1 auth)


def _fake_request(*, xff=None, peer="10.0.0.1"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(
        headers=SimpleNamespace(get=headers.get),
        client=SimpleNamespace(host=peer),
    )


def test_client_ip_uses_trusted_rightmost_hop(monkeypatch):
    """With one trusted proxy, the real client is the LAST XFF entry --
    the leftmost is whatever the client themselves sent and is ignored."""
    from app import rate_limit

    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_hops", 1)
    # Client forges a leading hop; the proxy appends the true peer.
    ip = rate_limit.client_ip(_fake_request(xff="1.2.3.4, 203.0.113.9"))
    assert ip == "203.0.113.9"


def test_client_ip_spoof_cannot_change_bucket(monkeypatch):
    """Rotating the forged leftmost hop must NOT change the derived IP --
    otherwise the rate-limit bucket is trivially reset per request."""
    from app import rate_limit

    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_hops", 1)
    a = rate_limit.client_ip(_fake_request(xff="99.99.99.1, 203.0.113.9"))
    b = rate_limit.client_ip(_fake_request(xff="88.88.88.2, 203.0.113.9"))
    assert a == b == "203.0.113.9"


def test_client_ip_zero_hops_uses_socket_peer(monkeypatch):
    from app import rate_limit

    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_hops", 0)
    assert rate_limit.client_ip(_fake_request(xff="1.2.3.4", peer="10.0.0.7")) == "10.0.0.7"


# ---------------------------------------------------------------- input bounds (F4 injection)


def test_wizard_food_step_rejects_oversized_list(client, db, loft):
    booking = _booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    import uuid

    huge = [{"menu_item_id": str(uuid.uuid4()), "quantity": 1} for _ in range(101)]
    resp = client.post(f"/w/{session.access_token}/food", json={"platters": huge})
    assert resp.status_code == 422  # bounded at 100 per category


# ---------------------------------------------------------------- hardening headers


def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "max-age=" in resp.headers.get("strict-transport-security", "")


def test_api_docs_closed_by_default(client):
    """A private booking system shouldn't publish its full route map."""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
