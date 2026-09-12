"""A Payment Link is deactivated through the account that minted it.

`create_payment_link` records the account alongside the link id and says
why, in a comment directly above the return: "a link can only ever be
deactivated through the account that minted it -- and once there are two,
'whichever key is current' is not that account".

Fifty lines below, `deactivate_payment_links` resolved ONE key for the
whole invoice and presented it for every link, never reading the account it
had been told to read. A wrong comment sitting on top of the very code it
describes.

WHAT IT COSTS. Stripe answers "No such payment link", `stripe.StripeError`
is caught and logged, the loop moves on -- and the link stays live and
payable. That is the Sophie Mavridis incident of 2026-09-04 exactly: a card
link still working after the offer behind it had been superseded by
somebody else confirming the same room and night.

The reach is any invoice holding a link minted under an account that is not
the one the venue's key resolves to today: a venue whose Stripe key is
repointed, and every link minted before the `secret_key_for` fix went in.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe

from app.models import Space, Venue
from app.models.invoice import InvoiceType
from app.services import invoicing, stripe_integration
from app.services.booking import create_booking


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_HAMILTON")
    monkeypatch.setenv("STRIPE_SECRET_KEY_ENTRANCE", "sk_test_NICETRY")
    monkeypatch.setattr(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_HAMILTON")


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
        stripe_secret_key_env="STRIPE_SECRET_KEY_ENTRANCE",
        stripe_account_id="acct_NICETRY",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


def _invoice(db, space, name):
    booking = create_booking(
        db, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    invoice = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    return invoice


def _keys_presented(invoice, link_ids=None):
    """Every api_key deactivation actually hands to Stripe, in order."""
    seen = []

    def _modify(ident, active=False, api_key=None):
        seen.append((ident, api_key))
        return {"id": ident}

    with patch.object(stripe.PaymentLink, "modify", side_effect=_modify):
        stripe_integration.deactivate_payment_links(invoice, link_ids)
    return seen


# --- the fix ---------------------------------------------------------------


def test_a_link_minted_in_another_account_is_closed_with_that_accounts_key(
    db, hamilton, loft, entrance, keys
):
    """THE one. An Entrance invoice holding a link that was minted in
    Hamilton's account -- which is the state every link created before the
    secret_key_for fix is in -- must be closed with Hamilton's key."""
    hamilton.stripe_account_id = "acct_HAMILTON"
    db.flush()
    invoice = _invoice(db, entrance.spaces[0], "Entrance Function")
    invoice.stripe_payment_link_ids = [{"id": "plink_STRAY", "account": "acct_HAMILTON"}]
    db.flush()

    seen = _keys_presented(invoice)

    assert seen == [("plink_STRAY", "sk_test_HAMILTON")], seen


def test_each_link_gets_its_own_account_key(db, hamilton, loft, entrance, keys):
    """Two links, two accounts, one invoice. One key for the invoice could
    only ever close one of them."""
    hamilton.stripe_account_id = "acct_HAMILTON"
    db.flush()
    invoice = _invoice(db, entrance.spaces[0], "Entrance Two Links")
    invoice.stripe_payment_link_ids = [
        {"id": "plink_OLD", "account": "acct_HAMILTON"},
        {"id": "plink_NEW", "account": "acct_NICETRY"},
    ]
    db.flush()

    seen = dict(_keys_presented(invoice))

    assert seen["plink_OLD"] == "sk_test_HAMILTON"
    assert seen["plink_NEW"] == "sk_test_NICETRY"


def test_an_account_no_venue_claims_is_refused_rather_than_guessed(
    db, hamilton, loft, entrance, keys, caplog
):
    """The repointed-key case. Presenting a key we already know is wrong
    would log "could not deactivate" as if Stripe were flaky; the truth is
    that no key here can close it and somebody has to open the dashboard."""
    invoice = _invoice(db, entrance.spaces[0], "Entrance Orphan Link")
    invoice.stripe_payment_link_ids = [{"id": "plink_ORPHAN", "account": "acct_LONG_GONE"}]
    db.flush()

    with caplog.at_level("ERROR"):
        seen = _keys_presented(invoice)

    assert seen == [], "a key was presented for an account nothing claims"
    assert "STILL PAYABLE" in caplog.text
    assert "acct_LONG_GONE" in caplog.text


# --- and the ordinary cases still work -------------------------------------


def test_a_link_with_no_account_recorded_uses_the_invoices_own_venue(
    db, hamilton, loft, entrance, keys
):
    """Entries written before the account was stored. create_payment_link
    has always minted with the invoice venue's own key, so that venue IS
    the answer here -- not a guess, and not Hamilton by default."""
    invoice = _invoice(db, entrance.spaces[0], "Entrance Bare Entry")
    invoice.stripe_payment_link_ids = ["plink_BARE"]
    db.flush()

    assert _keys_presented(invoice) == [("plink_BARE", "sk_test_NICETRY")]


def test_hamiltons_own_links_still_close(db, hamilton, loft, keys):
    """The other direction, so a change that refused everything could not
    pass the tests above on its own."""
    invoice = _invoice(db, loft, "Hamilton Function")
    invoice.stripe_payment_link_ids = ["plink_HAM"]
    db.flush()

    assert _keys_presented(invoice) == [("plink_HAM", "sk_test_HAMILTON")]


def test_one_unclosable_link_does_not_stop_the_others(db, hamilton, loft, entrance, keys):
    """Cancelling an invoice has to succeed even when one link cannot be
    reached. A refusal that aborted the loop would leave MORE links live
    than the bug did."""
    hamilton.stripe_account_id = "acct_HAMILTON"
    db.flush()
    invoice = _invoice(db, entrance.spaces[0], "Entrance Mixed Links")
    invoice.stripe_payment_link_ids = [
        {"id": "plink_ORPHAN", "account": "acct_LONG_GONE"},
        {"id": "plink_GOOD", "account": "acct_NICETRY"},
    ]
    db.flush()

    seen = _keys_presented(invoice)

    assert seen == [("plink_GOOD", "sk_test_NICETRY")]


def test_a_venue_with_no_key_reports_the_link_rather_than_returning_silently(
    db, hamilton, loft, entrance, keys, monkeypatch, caplog
):
    """It used to resolve one key up front and `return` on
    StripeNotConfigured -- every link on the invoice abandoned, nothing
    attempted and nothing said."""
    monkeypatch.delenv("STRIPE_SECRET_KEY_ENTRANCE", raising=False)
    invoice = _invoice(db, entrance.spaces[0], "Entrance Unconfigured")
    invoice.stripe_payment_link_ids = [{"id": "plink_NOKEY", "account": "acct_NICETRY"}]
    db.flush()

    with caplog.at_level("ERROR"):
        seen = _keys_presented(invoice)

    assert seen == []
    assert "plink_NOKEY" in caplog.text and "STILL PAYABLE" in caplog.text
