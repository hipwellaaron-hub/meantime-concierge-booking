"""A venue with no Stripe key of its own does not get Hamilton's.

`secret_key_for` resolved the key by the environment-variable NAME on the
venue row, and when the row named nothing it fell through to the
process-wide `STRIPE_SECRET_KEY`. The comment defended that: the only venue
with an empty column is the un-migrated one, and that is Hamilton.

True with one venue. False with two -- and a second row with that column
NULL is precisely what the setup sequence produces, because filling
`stripe_account_id` is deliberately deferred to its own step so the rollback
window stays open.

WHAT THAT COSTS, and why this is the most serious thing in the venue work:
a Nice Try Events Pty Ltd invoice mints a live Payment Link inside Meantime
Pty Ltd's Stripe account. The client pays. That account signs its own
completion event, so `construct_event` verifies it and the payment records
as successful. Nothing raises, nothing logs, and the money is in the wrong
company's bank account.

`assert_key_belongs_to` is no help in that window: it returns early while
`stripe_account_id` is NULL, which is the same window.
"""
import os

import pytest

from app.models import Venue
from app.services.stripe_integration import (
    LEGACY_STRIPE_VENUE_SLUG,
    StripeNotConfigured,
    is_configured_for,
    secret_key_for,
)


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setitem(os.environ, "STRIPE_SECRET_KEY", "sk_test_HAMILTONS_OWN_KEY")
    monkeypatch.delitem(os.environ, "STRIPE_SECRET_KEY_ENTRANCE", raising=False)


def _venue(slug, *, key_env=None):
    return Venue(name=slug.title(), slug=slug, stripe_secret_key_env=key_env)


# --- the refusal -----------------------------------------------------------


def test_a_second_venue_with_no_key_named_is_refused(keys):
    """THE one. Before this it returned Hamilton's key and said nothing."""
    with pytest.raises(StripeNotConfigured) as exc:
        secret_key_for(_venue("entrance"))

    assert "entrance" in str(exc.value)
    assert "another company's account" in str(exc.value)


def test_the_refusal_never_leaks_the_key_it_refused_to_use(keys):
    """The message goes into a log and onto a screen."""
    with pytest.raises(StripeNotConfigured) as exc:
        secret_key_for(_venue("entrance"))

    assert "sk_test_HAMILTONS_OWN_KEY" not in str(exc.value)


def test_a_venue_naming_a_variable_that_is_not_set_is_also_refused(keys):
    """The other half of the same rule: naming a key you have not set must
    not fall back either."""
    with pytest.raises(StripeNotConfigured) as exc:
        secret_key_for(_venue("entrance", key_env="STRIPE_SECRET_KEY_ENTRANCE"))

    assert "STRIPE_SECRET_KEY_ENTRANCE" in str(exc.value)


def test_the_card_option_simply_disappears_rather_than_erroring(keys):
    """What the client actually sees. is_configured_for is what the invoice
    page asks, so an unconfigured venue shows bank details and no card
    button -- not a 500 on somebody's invoice."""
    assert is_configured_for(_venue("entrance")) is False


# --- and Hamilton keeps working --------------------------------------------


def test_hamilton_with_an_empty_column_still_resolves(keys):
    """The rollback window. Hamilton was taking cards before venues had a
    column to name a key in, so its row may legitimately still be NULL and
    the process-wide variable really is its key. Named by slug, so it is a
    decision about Hamilton rather than a rule about absence."""
    assert secret_key_for(_venue(LEGACY_STRIPE_VENUE_SLUG)) == "sk_test_HAMILTONS_OWN_KEY"
    assert is_configured_for(_venue(LEGACY_STRIPE_VENUE_SLUG)) is True


def test_a_venue_that_names_its_own_variable_gets_that_one(keys, monkeypatch):
    """And the normal path once The Entrance is set up properly -- proving
    the refusal above is about the empty column, not about the venue."""
    monkeypatch.setitem(os.environ, "STRIPE_SECRET_KEY_ENTRANCE", "sk_test_NICE_TRY_EVENTS")

    key = secret_key_for(_venue("entrance", key_env="STRIPE_SECRET_KEY_ENTRANCE"))

    assert key == "sk_test_NICE_TRY_EVENTS"
    assert key != os.environ["STRIPE_SECRET_KEY"]


def test_hamilton_naming_its_own_variable_is_not_treated_as_legacy(keys, monkeypatch):
    """Once Hamilton's row names a variable, that variable is used -- the
    slug exemption applies only to an EMPTY column, so filling the row in
    cannot be silently ignored."""
    monkeypatch.setitem(os.environ, "STRIPE_SECRET_KEY_HAMILTON", "sk_test_HAMILTON_EXPLICIT")

    assert secret_key_for(
        _venue(LEGACY_STRIPE_VENUE_SLUG, key_env="STRIPE_SECRET_KEY_HAMILTON")
    ) == "sk_test_HAMILTON_EXPLICIT"
