"""A venue can mint payment links and be unable to verify the payments.

`is_configured_for` answers "can this venue take a card". Nothing answered
the other half: can the completion event its own Stripe account posts back
actually be VERIFIED. A venue with a good key and no signing secret set
mints a working link, the client pays, and `_signing_secret_for` refuses
the webhook -- correctly, and with a clear log line.

But the loudness lands in Stripe's delivery history and a log file. Stripe
retries for about three days and then drops the event. What is left is a
client charged, an invoice that still says unpaid, and nothing raised on
either side. That is the failure mode the webhook module's own docstring
describes, and it was only ever detectable after the money.

`stripe_webhook_ready` asks it before the money, on /healthz, degrading.

EVERY PROBE PATCHES is_configured_for TO TRUE. In the test environment no
venue has a Stripe key, so without the patch no venue is asked, the fold
over an empty set is true, and the boolean is green whatever the code does
-- the same shape as the preview test that passed because Stripe was
unconfigured, and the same patch its sibling
test_healthz_reports_an_unpinned_stripe_account.py uses.

NOT IN THE DIGEST, deliberately: this reads environment variables and the
digest runs in a different Railway service which holds no Stripe variables
at all. An env answer is only true about the process that answers it.
"""
from unittest.mock import patch

import pytest

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


# --- the helper ---------------------------------------------------------


def test_a_named_variable_that_is_set_is_ready(hamilton, monkeypatch):
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_PROBE", "whsec_probe")

    assert stripe_integration.webhook_secret_configured_for(hamilton) is True


def test_a_named_variable_that_is_not_set_is_not_ready(hamilton, monkeypatch):
    """THE one. The venue row looks complete -- it names its variable, so
    the readiness check's column list is satisfied -- and the variable does
    not exist in this environment."""
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET_PROBE", raising=False)

    assert stripe_integration.webhook_secret_configured_for(hamilton) is False


@pytest.mark.parametrize("named", [None, "", "   "])
def test_naming_nothing_is_not_ready(hamilton, named, monkeypatch):
    """No fallback to the shared secret. A venue that has not named its own
    variable is not entitled to another company's -- its account signs with
    its own secret, so borrowing would fail verification on every event
    anyway, and the silent version of that is what this exists to stop.

    THE SHARED SECRET IS SET FOR THIS PROBE, and that is the whole probe.
    Without it, a fallback to DEFAULT_STRIPE_WEBHOOK_SECRET_ENV also
    answers False -- because that variable is unset in the test
    environment, not because the code refused. The mutation putting the
    fallback back survived exactly that way on 2026-09-14.
    """
    monkeypatch.setenv(stripe_integration.DEFAULT_STRIPE_WEBHOOK_SECRET_ENV, "whsec_shared")
    hamilton.stripe_webhook_secret_env = named

    assert stripe_integration.webhook_secret_configured_for(hamilton) is False


def test_a_hand_typed_name_with_spaces_around_it_still_resolves(hamilton, monkeypatch):
    """The column is typed in by hand. " STRIPE_WEBHOOK_SECRET_ENTRANCE "
    is not a variable anybody has set, so an unstripped lookup misses it
    and refuses a venue that is correctly configured apart from two spaces.

    This is also what keeps the strip HONEST: the webhook endpoint strips
    the same column the same way, so a green answer here cannot sit over an
    endpoint that refuses every event.
    """
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_PROBE", "whsec_probe")
    hamilton.stripe_webhook_secret_env = "  STRIPE_WEBHOOK_SECRET_PROBE  "

    assert stripe_integration.webhook_secret_configured_for(hamilton) is True


def test_the_helper_resolves_the_same_variable_the_endpoint_does(hamilton, monkeypatch):
    """Structural-ish, and the point of the whole check: a second opinion
    about which variable holds the secret would be worse than no check,
    because it would read green while the endpoint refused."""
    import os

    from app.api import webhooks

    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_PROBE", "whsec_probe")

    assert stripe_integration.webhook_secret_configured_for(hamilton) is True
    assert os.environ.get(hamilton.stripe_webhook_secret_env) == "whsec_probe"
    # The endpoint's own resolution is `os.environ.get(name)` on the same
    # column (webhooks._signing_secret_for). Asserted on the source rather
    # than by driving a signed webhook, because the two are equivalent by
    # construction and a behavioural probe here would only prove that
    # os.environ works.
    import inspect

    endpoint = inspect.getsource(webhooks._signing_secret_for)
    helper = inspect.getsource(stripe_integration.webhook_secret_configured_for)

    resolution = '(getattr(venue, "stripe_webhook_secret_env", None) or "").strip()'
    assert resolution in endpoint, (
        "the webhook endpoint no longer resolves the variable the way this check "
        "does -- one of them will read green while the other refuses"
    )
    assert resolution in helper
    assert "os.environ.get(name)" in endpoint


# --- the endpoint -------------------------------------------------------


def test_healthz_reports_and_degrades_on_an_unverifiable_venue(db, hamilton, monkeypatch):
    # stripe_account_id SET, deliberately. Without it stripe_account_pinned
    # is false and the endpoint is degraded whatever this check says -- the
    # mutation removing this check from the status expression survived
    # exactly that way on 2026-09-14.
    hamilton.stripe_account_id = "acct_probe"
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET_PROBE", raising=False)
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        body = _healthz(db)

    assert "stripe_webhook_ready" in body["checks"], "/healthz does not report the check at all"
    assert body["checks"]["stripe_webhook_ready"] is False
    assert body["status"] == "degraded"


def test_healthz_reads_ready_when_the_secret_is_set(db, hamilton, monkeypatch):
    """The positive control. Without it every probe above could be passing
    on a check hardwired to False."""
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_PROBE", "whsec_probe")
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        body = _healthz(db)

    assert body["checks"]["stripe_webhook_ready"] is True


def test_a_venue_that_cannot_take_a_card_is_not_asked(db, hamilton, monkeypatch):
    """A venue with no Stripe key at all is not taking money yet and has
    nothing to verify -- counting it would sit the endpoint amber over a
    venue that is deliberately half set up, which is what the set-up
    section of the digest is for."""
    hamilton.stripe_webhook_secret_env = None
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=False):
        body = _healthz(db)

    assert body["checks"]["stripe_webhook_ready"] is True


def test_the_endpoint_names_no_variable(db, hamilton, monkeypatch):
    """Public endpoint. The NAME of a company's signing-secret variable is
    a hint about its infrastructure, and this endpoint's docstring forbids
    leaking config."""
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET_PROBE", raising=False)
    db.flush()

    with patch.object(stripe_integration, "is_configured_for", return_value=True):
        blob = repr(_healthz(db))

    assert "STRIPE_WEBHOOK_SECRET_PROBE" not in blob
    assert "whsec" not in blob


# --- and whether the card it takes is a real one ------------------------


def test_a_live_key_reads_live(db, hamilton, monkeypatch):
    """The three Stripe facts only mean something together: mode, the
    signing secret being present, and the account being pinned. The
    checklist calls the gap between them "the failure mode that matters
    most, because it's invisible" -- a live key with a test endpoint's
    secret charges a real card and never records the payment."""
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_PROBE"
    monkeypatch.setenv("STRIPE_SECRET_KEY_PROBE", "sk_live_probe")
    db.flush()

    body = _healthz(db)

    assert body["checks"]["stripe_live_mode"] is True


def test_a_test_key_reads_not_live(db, hamilton, monkeypatch):
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_PROBE"
    monkeypatch.setenv("STRIPE_SECRET_KEY_PROBE", "sk_test_probe")
    db.flush()

    body = _healthz(db)

    assert body["checks"]["stripe_live_mode"] is False


def test_test_mode_does_not_degrade_the_endpoint(db, hamilton, monkeypatch):
    """Deliberately asserted, because the alternative was considered and
    rejected: test mode is a real state a venue is in while being set up,
    and an endpoint amber over a deliberate state is one people stop
    reading. Everything else that could degrade is neutralised here so the
    mode is the only variable."""
    hamilton.stripe_account_id = "acct_probe"
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_PROBE"
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_PROBE", "whsec_probe")
    db.flush()

    monkeypatch.setenv("STRIPE_SECRET_KEY_PROBE", "sk_live_probe")
    live_status = _healthz(db)["status"]

    monkeypatch.setenv("STRIPE_SECRET_KEY_PROBE", "sk_test_probe")
    test_body = _healthz(db)

    assert test_body["checks"]["stripe_live_mode"] is False
    assert test_body["status"] == live_status, (
        "test mode moves /healthz between ok and degraded"
    )


def test_a_venue_with_no_key_is_not_counted(db, hamilton):
    """Same rule as the webhook check beside it: a venue that cannot take a
    card is not yet answering this question, and folding it in would sit the
    page on a venue deliberately half set up."""
    hamilton.stripe_secret_key_env = None
    db.flush()

    assert _healthz(db)["checks"]["stripe_live_mode"] is True


def test_the_endpoint_leaks_no_key(db, hamilton, monkeypatch):
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_PROBE"
    monkeypatch.setenv("STRIPE_SECRET_KEY_PROBE", "sk_live_probe")
    db.flush()

    blob = repr(_healthz(db))

    assert "sk_live" not in blob and "STRIPE_SECRET_KEY_PROBE" not in blob
