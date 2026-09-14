"""A drafting failure stops being invisible.

Aaron, 2026-09-14: "If it fails without telling anyone in shadow mode it
will fail without telling anyone in production."

The audit's sharpest point about the AI drafts: the ClaudeUnavailable
branch wrote ONE database row and returned. No log line, no notification,
no health signal. The two failures sitting on the drafts page -- an HTTP
401 and a transport error -- produced zero application log output. The
generic `except Exception` beside it has always logged; the specific
branch, which is the one a credential problem takes, did not.

And /healthz checked five things, none of them the model, even though the
identical degraded-on-a-failure-count pattern was already implemented in
the same function for enquiry notifications.

BLOCKED AND SKIPPED ARE NOT FAILURES. A gate declining to draft is the
system working as designed, and counting those would leave the endpoint
permanently degraded -- which is the same as no signal at all.
"""
import datetime as dt

from app.models.enquiry_draft import EnquiryDraft
from app.services import drafting
from app.services.drafting import STATUS_FAILED
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Draft Failure"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _attempt(db, booking, status, *, hours_ago=0, reason=None):
    row = EnquiryDraft(
        booking_id=booking.id, status=status, trigger="enquiry",
        failure_reason=reason,
    )
    db.add(row)
    db.flush()
    if hours_ago:
        row.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
        db.flush()
    return row


# --- the counter ------------------------------------------------------------


def test_a_recent_failure_is_counted(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZDRAFTFAIL Recent")
    _attempt(db, booking, STATUS_FAILED, reason="The model returned HTTP 401.")

    assert drafting.recent_failure_count(db) >= 1


def test_blocked_and_skipped_are_not_failures(db, hamilton, loft, contact):
    """A gate declining to draft is the system working. Counting those
    would leave /healthz permanently degraded, which is no signal at all."""
    booking = _booking(db, loft, contact, name="ZZDRAFTFAIL Gates")
    before = drafting.recent_failure_count(db)
    _attempt(db, booking, drafting.STATUS_BLOCKED)
    _attempt(db, booking, drafting.STATUS_SKIPPED)

    assert drafting.recent_failure_count(db) == before


def test_an_old_failure_falls_out_of_the_window(db, hamilton, loft, contact):
    """A 401 fixed last week must not hold the endpoint degraded forever."""
    booking = _booking(db, loft, contact, name="ZZDRAFTFAIL Old")
    before = drafting.recent_failure_count(db)
    _attempt(db, booking, STATUS_FAILED, hours_ago=72, reason="ancient")

    assert drafting.recent_failure_count(db) == before


# --- the health signal ------------------------------------------------------


def test_healthz_reports_drafting_and_goes_degraded(db, hamilton, loft, contact):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    booking = _booking(db, loft, contact, name="ZZDRAFTFAIL Health")
    _attempt(db, booking, STATUS_FAILED, reason="The model returned HTTP 401.")

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert "ai_drafting_failing" in body["checks"], "/healthz does not report the model at all"
    assert body["checks"]["ai_drafting_failing"] is True
    assert body["status"] == "degraded"


def test_healthz_is_ok_with_no_drafting_failures(db, hamilton, loft, contact):
    """It must be able to say ok, or it is not a signal."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()

    assert resp.json()["checks"]["ai_drafting_failing"] is False


# --- and the log line that was missing --------------------------------------


def test_an_unavailable_model_writes_a_log_line(db, hamilton, loft, contact, caplog):
    """The branch that produced no output at all. Asserted through the
    real failure path rather than by reading the source, because a log call
    that is never reached is the same as no log call."""
    import logging

    from app.services import claude_client

    booking = _booking(db, loft, contact, name="ZZDRAFTFAIL Logged")

    def _boom(*args, **kwargs):
        raise claude_client.ClaudeUnavailable("The model returned HTTP 401.")

    # FOUR things refuse before this branch, and each one would make the
    # assertion pass for the wrong reason -- drafting switched off, no API
    # key, the gate declining, and venue_profile. They are all driven to
    # the state that reaches complete(), which is the call the branch
    # actually wraps. Proved by running it: with the drafting switch left
    # off, the function returns SKIPPED and never logs.
    from app.services import ai_access, draft_gate

    row = ai_access.get_settings_row(db)
    was_drafting = row.drafting_enabled
    row.drafting_enabled = True
    db.flush()

    class _Pass:
        should_draft = True
        codes: list = []
        facts: dict = {}

        def as_note(self):
            return ""

    original_complete = drafting.claude_client.complete
    original_configured = drafting.claude_client.is_configured
    original_evaluate = drafting.draft_gate.evaluate
    original_ground = drafting._ground
    drafting.claude_client.complete = _boom
    drafting.claude_client.is_configured = lambda: True
    drafting.draft_gate.evaluate = lambda *a, **k: _Pass()
    drafting._ground = lambda *a, **k: ({"client_asked_for_figures": False}, True)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.drafting"):
            drafting.draft_for_booking(db, booking.id, trigger="enquiry")
    finally:
        drafting.claude_client.complete = original_complete
        drafting.claude_client.is_configured = original_configured
        drafting.draft_gate.evaluate = original_evaluate
        drafting._ground = original_ground
        row.drafting_enabled = was_drafting
        db.flush()

    assert any("401" in r.getMessage() for r in caplog.records), (
        "an unavailable model still produces no log output"
    )
