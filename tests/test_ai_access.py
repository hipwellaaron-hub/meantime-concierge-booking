"""The AI boundary: credential, scoping, kill switches, rate limits, log.

These tests are about what must NOT be reachable. The brief's whole
security model is that a Tier 3 action has no route (section 2), so the
most important assertion here is the dullest one: attempting a
consequential action returns 404, because nothing is listening.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AiRequestLog, AiSettings, BookingEvent
from app.services import ai_access

TOKEN = "test-ai-token-do-not-use-in-production"


@pytest.fixture()
def ai_client(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    monkeypatch.setattr(settings, "ai_writes_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client
    finally:
        app.dependency_overrides.clear()


# --- credential ---------------------------------------------------------


def test_no_credential_is_refused(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/api/ai/pipeline")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_wrong_credential_is_refused(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/ai/pipeline", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_unset_token_refuses_everything(db, hamilton, monkeypatch):
    """A deployment that never configured a token is closed, not open."""
    monkeypatch.setattr(settings, "ai_api_token", "")
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/ai/pipeline", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_valid_credential_is_admitted(ai_client):
    resp = ai_client.get("/api/ai/pipeline")
    assert resp.status_code == 200
    assert "as_of" in resp.json()


# --- kill switches ------------------------------------------------------


def test_database_kill_switch_disables_reads_instantly(ai_client, db):
    assert ai_client.get("/api/ai/pipeline").status_code == 200
    ai_access.set_access_enabled(db, False, actor="staff:test")
    resp = ai_client.get("/api/ai/pipeline")
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


def test_env_backstop_disables_even_when_database_says_yes(ai_client, db, monkeypatch):
    ai_access.set_access_enabled(db, True, actor="staff:test")
    monkeypatch.setattr(settings, "ai_access_enabled", False)
    assert ai_client.get("/api/ai/pipeline").status_code == 503


def test_writes_switch_is_independent_of_reads(db, hamilton):
    ai_access.set_writes_enabled(db, False, actor="staff:test", reason="testing")
    assert ai_access.access_enabled(db) is True  # reads continue
    assert ai_access.writes_enabled(db) is False
    row = db.get(AiSettings, 1)
    assert row.writes_disabled_reason == "testing"
    assert row.writes_disabled_at is not None


# --- write rate limiting (database-backed, survives a restart) ----------


def test_write_budget_counts_from_the_database(db, hamilton):
    from app.models import AiRequestKind

    for _ in range(3):
        ai_access.log_request(
            db, kind=AiRequestKind.write, endpoint="/api/ai/test", method="POST"
        )
    budget = ai_access.write_budget(db)
    assert budget["hour_used"] == 3
    assert budget["day_used"] == 3


def test_exceeding_the_hourly_limit_auto_disables_writes(db, hamilton, monkeypatch):
    from app.models import AiRequestKind

    monkeypatch.setattr(settings, "ai_write_rate_per_hour", 2)
    for _ in range(2):
        ai_access.log_request(
            db, kind=AiRequestKind.write, endpoint="/api/ai/test", method="POST"
        )

    with pytest.raises(ai_access.AiAccessError) as excinfo:
        ai_access.enforce_write_budget(db)
    assert excinfo.value.status == 429

    # The auto-disable is persisted, so a restart cannot silently re-enable it.
    assert ai_access.writes_enabled(db) is False
    assert "last hour" in db.get(AiSettings, 1).writes_disabled_reason


def test_reads_do_not_consume_the_write_budget(db, hamilton):
    from app.models import AiRequestKind

    for _ in range(5):
        ai_access.log_request(db, kind=AiRequestKind.read, endpoint="/api/ai/pipeline", method="GET")
    assert ai_access.write_budget(db)["hour_used"] == 0


# --- audit separation ---------------------------------------------------


def test_reads_are_logged_but_never_into_the_booking_audit_trail(ai_client, db, booking):
    """BookingEvent is what staff read and the dashboard renders. A few
    hundred reads a day must not bury the writes in it."""
    events_before = db.query(BookingEvent).count()
    logs_before = db.query(AiRequestLog).count()

    ai_client.get("/api/ai/pipeline")
    db.expire_all()

    assert db.query(AiRequestLog).count() == logs_before + 1
    assert db.query(BookingEvent).count() == events_before  # untouched


def test_read_log_records_endpoint_and_params(ai_client, db):
    ai_client.get("/api/ai/availability?date=2026-11-28")
    db.expire_all()
    entry = db.query(AiRequestLog).order_by(AiRequestLog.at.desc()).first()
    assert entry.endpoint == "/api/ai/availability"
    assert entry.method == "GET"
    assert entry.params["date"] == "2026-11-28"
    assert entry.actor == "ai:claude"
    assert entry.kind == "read"


# --- the boundary itself ------------------------------------------------


CONSEQUENTIAL_ATTEMPTS = [
    ("POST", "/api/ai/bookings/00000000-0000-0000-0000-000000000000/status"),
    ("POST", "/api/ai/bookings/00000000-0000-0000-0000-000000000000/documents"),
    ("POST", "/api/ai/bookings/00000000-0000-0000-0000-000000000000/invoices"),
    ("POST", "/api/ai/bookings/00000000-0000-0000-0000-000000000000/payments"),
    ("POST", "/api/ai/documents/00000000-0000-0000-0000-000000000000/send"),
    ("DELETE", "/api/ai/bookings/00000000-0000-0000-0000-000000000000"),
    ("POST", "/api/ai/catalogue"),
    ("GET", "/api/ai/documents/00000000-0000-0000-0000-000000000000/pdf"),
]


@pytest.mark.parametrize("method,path", CONSEQUENTIAL_ATTEMPTS)
def test_tier_three_actions_do_not_exist(ai_client, method, path):
    """404, not 403: the endpoint is physically absent, so it cannot be
    re-enabled by changing a policy or talking the model into it."""
    resp = ai_client.request(method, path)
    assert resp.status_code == 404, f"{method} {path} returned {resp.status_code}"


def test_credential_cannot_reach_admin_routes(ai_client):
    """Scoped to /api/ai/* -- the bearer token is meaningless elsewhere."""
    resp = ai_client.get("/admin/bookings", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)  # bounced to login, not admitted
