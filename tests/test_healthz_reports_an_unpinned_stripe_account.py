"""/healthz says whether every Stripe key is pinned to the account it should be.

stripe_integration.assert_key_belongs_to is the guard between a mis-keyed
credential and a payment recorded as successful for the wrong company. It
returns early when the venue has no stripe_account_id -- by design: the
row is meant to be NULL until somebody arms the guard by hand, a numbered
step in docs/stripe-go-live-checklist.md. That is a deliberate rollback
window, and it is deliberately excluded from the seed's unfilled-columns
report for the same reason.

So today the guard is a no-op, and nothing anywhere says so. A window that
nobody is reminded to close is the same as no guard. This is the
reminder: one folded boolean, true only when every venue that can mint a
payment link also names the account that key must belong to.

EVERY PROBE PATCHES is_configured_for TO TRUE. In the test environment no
venue has a Stripe key, so without the patch no venue is keyed, nothing
is unpinned, and the boolean is true whatever the code does -- the same
shape as the preview test that passed because Stripe was unconfigured.
"""
from unittest.mock import patch

from app.services import stripe_integration


def _healthz(db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        return TestClient(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()


def test_a_keyed_venue_with_no_account_id_reads_unpinned_and_degraded(db, hamilton):
    """THE one: Hamilton's shape on 2026-09-14 -- a live key, no account."""
    hamilton.stripe_account_id = None
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        body = _healthz(db)

    assert "stripe_account_pinned" in body["checks"], "/healthz does not report the guard at all"
    assert body["checks"]["stripe_account_pinned"] is False
    assert body["status"] == "degraded"


def test_a_keyed_venue_with_an_account_id_reads_pinned(db, hamilton):
    hamilton.stripe_account_id = "acct_hamilton"
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        body = _healthz(db)

    assert body["checks"]["stripe_account_pinned"] is True


def test_an_unkeyed_venue_is_not_unpinned(db, hamilton):
    """No key, nothing to pin. A second venue that has not been wired to
    Stripe at all must not hold the endpoint degraded."""
    hamilton.stripe_account_id = None
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=False):
        body = _healthz(db)

    assert body["checks"]["stripe_account_pinned"] is True


def test_the_response_names_no_account(db, hamilton):
    """Public endpoint: booleans only."""
    hamilton.stripe_account_id = "acct_hamilton"
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.main import app

        app.dependency_overrides[get_db] = lambda: db
        try:
            text = TestClient(app).get("/healthz").text
        finally:
            app.dependency_overrides.clear()

    assert "acct_hamilton" not in text
