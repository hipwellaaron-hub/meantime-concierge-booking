"""/healthz reports the three AI gates, and reports them as the live path sees them.

Aaron, 2026-09-14: "keep AI draft off for both venues, it's something we can
work on in a few months." That turns a transient switch into a DELIBERATE
long-lived state -- and a switch meant to stay off for months is exactly the
kind that comes back on without anyone noticing: a database restore, a
self-heal, a future session, a hand on the admin page.

Two things this asserts, and the second is the one worth having:

  * The gates appear on /healthz at all, so a change is visible on the page
    already being watched from outside the process.
  * ai_drafting_enabled is the EFFECTIVE gate -- the same expression
    drafting.draft_for_booking evaluates -- not the ai_settings column on
    its own. Reporting the column would read "drafting on" while the master
    switch above it was shut, which is a page that lies in the safe
    direction today and the unsafe one tomorrow.

And they are REPORTED, not degrading. A gate being open is a decision, not
a fault, and an endpoint that sits amber over a deliberate state is one
people stop reading.

THE GATES ARE GLOBAL. One ai_settings row (id=1) gates both companies, so
there is no per-venue answer to give and none is claimed.
"""

from app.services import ai_access, drafting


def _healthz(db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        return TestClient(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()


def test_the_three_gates_are_reported(db):
    body = _healthz(db)

    for gate in ("ai_access_enabled", "ai_writes_enabled", "ai_drafting_enabled"):
        assert gate in body["checks"], f"/healthz does not report {gate}"
        assert isinstance(body["checks"][gate], bool), f"{gate} is not a plain boolean"


def test_drafting_reads_off_when_the_master_switch_is_shut(db):
    """The state that would be misreported by the ai_settings column alone:
    drafting_enabled TRUE, access_enabled FALSE. Nothing drafts, and the
    page must say so."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = False
    row.drafting_enabled = True
    db.flush()

    body = _healthz(db)

    assert body["checks"]["ai_drafting_enabled"] is False, (
        "/healthz reports drafting ON while the master switch is shut -- it is "
        "reading the ai_settings column rather than the gate drafting.py evaluates"
    )
    # Which of the two is shut stays derivable.
    assert body["checks"]["ai_access_enabled"] is False


def test_the_page_agrees_with_what_drafting_actually_does(db, booking):
    """The positive control that makes the assertion above mean something:
    in the SAME state, the live path records SKIPPED. If these two ever
    disagree the page is the thing that is wrong."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = False
    row.drafting_enabled = True
    db.flush()

    reported = _healthz(db)["checks"]["ai_drafting_enabled"]
    draft = drafting.draft_for_booking(db, booking.id)

    assert draft is not None and draft.status == drafting.STATUS_SKIPPED
    assert reported is False, (
        "drafting skipped but /healthz says the gate is open"
    )


def test_both_open_reads_open(db):
    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.drafting_enabled = True
    db.flush()

    body = _healthz(db)

    assert body["checks"]["ai_drafting_enabled"] is True, (
        "both switches open and the page still says drafting is off -- the "
        "check would be vacuously true in every other probe here"
    )


def test_an_open_gate_does_not_degrade_the_endpoint(db):
    """Reported, not degraded. Deliberately asserted: the alternative was
    considered and rejected, and a later hand reaching for `or not
    ai_gate_open` in the status expression should have to delete a test
    that says why."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.writes_enabled = True
    row.drafting_enabled = True
    db.flush()

    open_status = _healthz(db)["status"]

    row.access_enabled = False
    row.writes_enabled = False
    row.drafting_enabled = False
    db.flush()

    shut_status = _healthz(db)["status"]

    assert open_status == shut_status, (
        "the AI gates move /healthz between ok and degraded -- a deliberate "
        "state must not sit the endpoint amber"
    )


def test_a_missing_settings_row_does_not_take_the_endpoint_down(db):
    """get_settings_row self-heals with everything OFF and commits. The
    endpoint must survive that rather than fall into its bare except and
    report a one-key `checks` dict, which is what a raise here would do."""
    row = db.get(ai_access.AiSettings, 1)
    if row is not None:
        db.delete(row)
        db.flush()

    body = _healthz(db)

    assert body["checks"].get("ai_access_enabled") is False
    assert "database" in body["checks"] and len(body["checks"]) > 1, (
        "the endpoint fell into its catch-all and reported nothing but the database"
    )


def test_the_gates_name_no_venue(db):
    """Same rule as the rest of this endpoint: it is public, and which
    companies operate here is not a fact a monitoring URL hands out."""
    body = _healthz(db)

    blob = repr(body).lower()
    for word in ("hamilton", "entrance", "meantime", "nice try"):
        assert word not in blob, f"/healthz leaks {word!r}"


def test_the_env_backstop_counts(db, monkeypatch):
    """ai_access reads two sources and either saying False wins. The
    database row is the kill switch; the env var is the backstop. A page
    that read the row alone would say the gate was open while the process
    it is reporting on had it shut."""
    from app.config import settings

    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.drafting_enabled = True
    db.flush()

    monkeypatch.setattr(settings, "ai_access_enabled", False)
    body = _healthz(db)

    assert body["checks"]["ai_access_enabled"] is False, (
        "/healthz reports the ai_settings row rather than ai_access.access_enabled, "
        "so the environment backstop is invisible on the page"
    )
    assert body["checks"]["ai_drafting_enabled"] is False
