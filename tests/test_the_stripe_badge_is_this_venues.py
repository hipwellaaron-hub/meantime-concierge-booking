"""The Stripe live/test badge reports the venue's own key.

`admin_ctx` puts `stripe_mode` on EVERY admin page, deliberately: its
comment says the risk it guards against -- "staff assumes real money is
moving" -- is not confined to the invoice screen. It was also the one
thing on a venue-scoped page that was not venue-scoped, because
`get_mode()` reads the process-wide `STRIPE_SECRET_KEY`.

Two companies means two keys, and one can be live while the other is not:

  * Hamilton live, The Entrance on a sandbox key -> The Entrance's pages
    show the green "Stripe" badge and no test banner, so a deposit is taken
    through a link that charges nothing and nobody waits for money that is
    never coming; and
  * the inverse is worse -- an amber "no real charges" banner over a page
    whose links charge real cards.
"""
import re

import pytest

from app.models import Space, Venue
from app.services import stripe_integration
from decimal import Decimal


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT", stripe_secret_key_env="STRIPE_SECRET_KEY_ENTRANCE",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


@pytest.fixture()
def hamilton_live_entrance_test(monkeypatch):
    """The shape that actually arrives: one venue trading on real cards
    while the other is still being set up."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_HAMILTON")
    monkeypatch.setenv("STRIPE_SECRET_KEY_ENTRANCE", "sk_test_NICETRY")
    monkeypatch.setattr(stripe_integration, "STRIPE_SECRET_KEY", "sk_live_HAMILTON")


# --- the helper ------------------------------------------------------------


def test_each_venue_reports_its_own_key(db, hamilton, entrance, hamilton_live_entrance_test):
    assert stripe_integration.mode_for(hamilton) is stripe_integration.StripeMode.live
    assert stripe_integration.mode_for(entrance) is stripe_integration.StripeMode.test


def test_a_venue_with_no_key_is_not_configured_rather_than_borrowing_one(
    db, hamilton, entrance, hamilton_live_entrance_test, monkeypatch
):
    """Same rule as secret_key_for: a venue nobody has finished setting up
    reports that, instead of inheriting whichever key the process holds."""
    monkeypatch.delenv("STRIPE_SECRET_KEY_ENTRANCE", raising=False)

    assert stripe_integration.mode_for(entrance) is stripe_integration.StripeMode.not_configured


def test_no_venue_in_scope_falls_back_to_the_process_key(hamilton_live_entrance_test):
    """The chooser and the login have no venue. They still need an answer,
    and the process key is the only one there is."""
    assert stripe_integration.mode_for(None) is stripe_integration.StripeMode.live


# --- what the page actually renders ----------------------------------------


def _badge(html: str) -> str:
    if re.search(r'class="badge green"[^>]*>Stripe<', html):
        return "live"
    if "Test mode" in html or "no real charges" in html:
        return "test"
    return "neither"


def test_a_test_mode_venue_does_not_show_the_live_badge(
    admin_client, db, hamilton, entrance, hamilton_live_entrance_test
):
    """THE one. The page renders, the band names The Entrance, and the
    badge above it was Hamilton's."""
    ham = admin_client.get("/admin/hamilton/", follow_redirects=True).text
    ent = admin_client.get("/admin/entrance/", follow_redirects=True).text

    assert _badge(ham) == "live", "Hamilton's own live badge went missing"
    assert _badge(ent) != "live", (
        "The Entrance's page showed the Stripe LIVE badge while its key is a test key"
    )


def test_a_live_venue_is_not_covered_by_the_other_ones_test_banner(
    admin_client, db, hamilton, entrance, monkeypatch
):
    """The inverse, and the more dangerous direction: an amber 'no real
    charges' banner over a page whose links charge real cards."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_HAMILTON")
    monkeypatch.setenv("STRIPE_SECRET_KEY_ENTRANCE", "sk_live_NICETRY")
    monkeypatch.setattr(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_HAMILTON")

    ent = admin_client.get("/admin/entrance/", follow_redirects=True).text

    assert "no real charges" not in ent, (
        "The Entrance's pages claimed no real charges while its key is live"
    )
    assert _badge(ent) == "live"
