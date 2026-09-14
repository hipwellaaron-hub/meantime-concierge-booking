"""The AI master gates can be turned off from a page, and heal shut.

Two findings from the 2026-09-14 sweep.

NO UI AT ALL. access_enabled and writes_enabled appeared in no template and
no route: the only ways to change them were a direct database write or the
env var override, and /healthz did not report them either. The reason they
live in the database rather than the environment is stated in
app/services/ai_access.py -- an env var "needs a ~90s rebuild on Railway,
which is not a kill switch". A kill switch reachable only with psql is not
one either.

THE SELF-HEAL OPENED BOTH GATES. get_settings_row recreated a missing row
with access_enabled=True AND writes_enabled=True, so restoring a database
without it and then hitting any AI endpoint silently opened the WRITE gate
with nobody deciding to.

That branch has never run on production -- migration a1c4f7e20b91 seeds the
row, verified 2026-09-14 -- so this is about a restored database, which is
exactly where a quiet "on" is least defensible: nobody is watching a
restore, and the safe failure for a write gate is closed.
"""
import re

import pytest

from app.models import AiSettings
from app.services import ai_access


@pytest.fixture(autouse=True)
def _restore_ai_settings(db):
    """ai_settings is a SEEDED SINGLETON on a SHARED test database, and
    every test here commits to it -- through get_settings_row, which
    commits, and through the route, which commits. A commit releases the
    conftest savepoint, so these writes escape the outer rollback and
    persist for every later test in the run.

    That is not hypothetical: it emptied the table on 2026-09-14 and took
    64 unrelated tests with it on the next full run. Snapshot in, restore
    out, whatever the test did."""
    row = db.get(AiSettings, 1)
    before = (
        (row.access_enabled, row.writes_enabled, row.drafting_enabled,
         row.drafts_visible, row.writes_disabled_at, row.writes_disabled_reason, row.updated_by)
        if row is not None else None
    )
    yield
    if before is None:
        return
    row = db.get(AiSettings, 1)
    if row is None:
        row = AiSettings(id=1)
        db.add(row)
    (row.access_enabled, row.writes_enabled, row.drafting_enabled,
     row.drafts_visible, row.writes_disabled_at, row.writes_disabled_reason,
     row.updated_by) = before
    db.commit()


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


# --- the self-heal ----------------------------------------------------------


def test_a_missing_settings_row_heals_shut(db, hamilton):
    """THE one. It used to heal with both gates open.

    RESTORED BY HAND IN A finally, and that is not belt-and-braces. This
    test deletes a SEEDED SINGLETON, and get_settings_row COMMITS -- which
    releases the conftest's savepoint, so the deletion escaped the outer
    rollback and emptied ai_settings for the whole shared test database.
    Sixty-four unrelated tests failed on the next full run because AI
    access was suddenly off for all of them. Proven, not theorised: that is
    exactly what happened on 2026-09-14.
    """
    existing = db.get(AiSettings, 1)
    before = (
        (existing.access_enabled, existing.writes_enabled,
         existing.drafting_enabled, existing.drafts_visible)
        if existing is not None else None
    )
    try:
        if existing is not None:
            db.delete(existing)
            db.flush()

        row = ai_access.get_settings_row(db)

        assert row.access_enabled is False, "a restored database silently re-opened AI access"
        assert row.writes_enabled is False, "a restored database silently re-opened AI WRITES"
    finally:
        if before is not None:
            row = db.get(AiSettings, 1)
            if row is None:
                row = AiSettings(id=1)
                db.add(row)
            (row.access_enabled, row.writes_enabled,
             row.drafting_enabled, row.drafts_visible) = before
            db.commit()


def test_the_heal_does_not_touch_an_existing_row(db, hamilton):
    """It only fires when the row is absent, which is why it never ran on
    production. An existing row's values must be left exactly alone."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.writes_enabled = True
    db.flush()

    again = ai_access.get_settings_row(db)

    assert again.access_enabled is True
    assert again.writes_enabled is True


# --- the UI that did not exist ----------------------------------------------


def test_the_page_offers_the_master_gates(admin_client, db, hamilton):
    page = admin_client.get("/admin/hamilton/drafts", follow_redirects=True)

    assert page.status_code == 200
    assert 'name="access_enabled"' in page.text, "no control for the AI access gate"
    assert 'name="writes_enabled"' in page.text, "no control for the AI writes gate"


def test_turning_access_off_from_the_page_takes_effect(admin_client, db, hamilton):
    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.writes_enabled = True
    db.flush()

    page = admin_client.get("/admin/hamilton/drafts", follow_redirects=True)
    admin_client.post(
        "/admin/hamilton/drafts/master-switches",
        data={"csrf_token": _csrf(page.text)},  # both checkboxes absent == off
        follow_redirects=False,
    )

    db.refresh(row)
    assert row.access_enabled is False
    assert row.writes_enabled is False
    assert ai_access.access_enabled(db) is False


def test_turning_it_off_records_when_and_by_whom(admin_client, db, hamilton):
    """The model has carried writes_disabled_at and writes_disabled_reason
    all along and nothing wrote them -- a gate that closes without recording
    why is the same shape as the view stamps fixed the same day."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = True
    row.writes_enabled = True
    row.writes_disabled_at = None
    db.flush()

    page = admin_client.get("/admin/hamilton/drafts", follow_redirects=True)
    admin_client.post(
        "/admin/hamilton/drafts/master-switches",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )

    db.refresh(row)
    assert row.writes_disabled_at is not None
    assert "AI access page" in (row.writes_disabled_reason or "")
    assert row.updated_by and row.updated_by.startswith("staff:")


def test_turning_both_back_on_works(admin_client, db, hamilton):
    """A switch that can only go one way is not a switch."""
    row = ai_access.get_settings_row(db)
    row.access_enabled = False
    row.writes_enabled = False
    db.flush()

    page = admin_client.get("/admin/hamilton/drafts", follow_redirects=True)
    admin_client.post(
        "/admin/hamilton/drafts/master-switches",
        data={"csrf_token": _csrf(page.text), "access_enabled": "on", "writes_enabled": "on"},
        follow_redirects=False,
    )

    db.refresh(row)
    assert row.access_enabled is True
    assert row.writes_enabled is True
