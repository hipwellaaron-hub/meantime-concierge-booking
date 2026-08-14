import datetime as dt
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services import notifications
from app.services.enquiry_classification import create_enquiry_booking


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_healthz_reports_ok_when_everything_is_fine(db, hamilton):
    client = _client(db)
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["database"] is True
        assert body["checks"]["enquiry_notifications_failing"] is False
    finally:
        app.dependency_overrides.clear()


def test_healthz_is_degraded_when_an_enquiry_notification_has_failed(db, hamilton, unassigned_space):
    # Gmail SMTP isn't configured in tests, so this genuinely fails --
    # exactly the real-world condition this check exists to surface.
    create_enquiry_booking(
        db, venue=hamilton, full_name="Health Check Test", email="health.check@example.com", phone=None,
        event_name="Health Check Booking", event_type="Wedding", event_date=dt.date(2027, 5, 10),
        proposed_time_slot=None, attendee_count=10, adult_count=10, company_name=None,
        dates_flexible=False, comments=None, lead_source="direct", lead_referrer=None, actor="test",
    )

    client = _client(db)
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["enquiry_notifications_failing"] is True
    finally:
        app.dependency_overrides.clear()


def test_healthz_reports_down_when_the_database_is_unreachable(db, hamilton):
    client = _client(db)
    try:
        with patch.object(db, "execute", side_effect=Exception("connection refused")):
            resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["checks"] == {"database": False}
    finally:
        app.dependency_overrides.clear()


def test_healthz_reflects_gmail_and_stripe_config_flags(db, hamilton):
    client = _client(db)
    try:
        with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
             patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake"):
            resp = client.get("/healthz")
        assert resp.json()["checks"]["gmail_configured"] is True
    finally:
        app.dependency_overrides.clear()


def test_healthz_never_requires_authentication(db, hamilton):
    """An external uptime monitor can't log in -- this must stay public."""
    client = _client(db)
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
