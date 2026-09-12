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


# --- the endpoint that exists to detect trouble must not fail open ----------


def test_healthz_is_degraded_when_there_is_no_venue_at_all(db):
    """THE fail-open. Until 2026-09-12 this read
    `filter_by(slug="hamilton").first()` -- `.first()` returns None where the
    eight `_venue()` helpers all use `.one()`, which raises -- and then
    reported notification_failures = 0 and status "ok".

    An external monitor would have shown a green light over a database with
    no venue in it.

    Hamilton is emptied INSIDE the test transaction, which the `db` fixture
    always rolls back -- not merely left unrequested, because the seeded row
    persists in the shared test database and the fixture would have found it
    anyway. That version of this test passed with the fix reverted.
    """
    from sqlalchemy import text as sql_text

    # The invoice registers go first: venue_invoice_counters holds a
    # foreign key to venues (f3d9b7c1a468), opened automatically the first
    # time a venue raises an invoice, so leaving it would make this DELETE
    # fail rather than empty the table.
    db.execute(sql_text("DELETE FROM venue_invoice_counters"))
    db.execute(sql_text("DELETE FROM spaces"))
    db.execute(sql_text("DELETE FROM venues"))
    db.flush()

    client = _client(db)
    try:
        resp = client.get("/healthz")

        assert resp.status_code == 200, "a monitoring endpoint must still answer"
        body = resp.json()
        assert body["status"] == "degraded", "an empty database reported healthy"
        assert body["checks"]["venues_present"] is False
    finally:
        app.dependency_overrides.clear()


def test_healthz_counts_a_second_venues_failures_too(db, hamilton, unassigned_space):
    """A second venue's failing enquiry notifications were invisible: only
    Hamilton was ever asked."""
    from decimal import Decimal

    from app.models import Space, Venue

    other = Venue(name="Meantime The Entrance", slug="entrance", reference_prefix="ENT")
    db.add(other)
    db.flush()
    db.add(Space(
        venue_id=other.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.add(Space(
        venue_id=other.id, name="Unassigned (pending triage)", capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    db.flush()

    # Gmail SMTP is not configured in tests, so this genuinely fails --
    # and it fails at the OTHER venue, not Hamilton.
    create_enquiry_booking(
        db, venue=other, full_name="Entrance Health Check", email="entrance.health@example.com",
        phone=None, event_name="Entrance Booking", event_type="Birthday",
        event_date=dt.date(2027, 6, 12),
        proposed_time_slot=None, attendee_count=40, adult_count=40, company_name=None,
        dates_flexible=False, comments=None, lead_source="direct", lead_referrer=None, actor="test",
    )
    db.flush()

    client = _client(db)
    try:
        body = client.get("/healthz").json()

        assert body["checks"]["enquiry_notifications_failing"] is True, (
            "a second venue's failing notifications did not reach the health check"
        )
        assert body["status"] == "degraded"
    finally:
        app.dependency_overrides.clear()


def test_healthz_names_no_venue(db, hamilton):
    """Public endpoint. Which companies operate here is not a fact a
    monitoring URL should hand out, and the module docstring already forbids
    returning anything beyond booleans and counts."""
    client = _client(db)
    try:
        text = client.get("/healthz").text.lower()

        assert "hamilton" not in text
        assert "entrance" not in text
        assert "meantime" not in text
    finally:
        app.dependency_overrides.clear()
