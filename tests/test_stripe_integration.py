import datetime as dt
import os
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe as stripe_sdk
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import stripe_integration
from app.services.invoicing import create_invoice, get_payment_summary, get_total_paid, mark_sent

TEST_WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests"


def _sign(payload_bytes: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload_bytes
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def _checkout_completed_event(*, invoice_id, payment_intent_id="pi_test_123", amount_total=50000) -> bytes:
    event = {
        "id": "evt_test_1",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "object": "checkout.session",
                "payment_intent": payment_intent_id,
                "amount_total": amount_total,
                "metadata": {stripe_integration.INVOICE_METADATA_KEY: str(invoice_id)},
            }
        },
    }
    return json.dumps(event).encode()


# --- _to_cents -----------------------------------------------------------


def test_to_cents_basic():
    assert stripe_integration._to_cents(Decimal("500.00")) == 50000


def test_to_cents_rounds_half_up():
    assert stripe_integration._to_cents(Decimal("10.005")) == 1001  # not banker's rounding to 1000


# --- get_mode --------------------------------------------------------------


def test_mode_not_configured_when_no_key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.not_configured


def test_mode_test_for_sk_test_key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_51AbCdEf1234567890"}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.test


def test_mode_test_for_restricted_test_key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "rk_test_51AbCdEf1234567890"}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.test


def test_mode_live_for_sk_live_key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_live_51AbCdEf1234567890"}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


def test_mode_live_for_restricted_live_key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "rk_live_51AbCdEf1234567890"}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


def test_mode_defaults_to_live_for_unrecognized_key_shape():
    """An unrecognized shape must fail toward "assume this is real money",
    never toward "assume it's safe" -- see get_mode's own docstring."""
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "some_future_key_format_stripe_hasnt_shipped_yet"}):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


# --- create_payment_link ---------------------------------------------------


def test_create_payment_link_raises_when_not_configured(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
        with pytest.raises(stripe_integration.StripeNotConfigured):
            stripe_integration.create_payment_link(invoice, Decimal("500.00"))


def test_create_payment_link_calls_stripe_with_invoice_metadata(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )

    class FakeLink:
        url = "https://checkout.stripe.com/fake-link"
        id = "plink_fake123"

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_fake"}):
        with patch.object(stripe_integration.stripe.PaymentLink, "create", return_value=FakeLink()) as mock_create:
            url, link_id, account = stripe_integration.create_payment_link(invoice, Decimal("500.00"))

    assert url == "https://checkout.stripe.com/fake-link"
    assert link_id == "plink_fake123"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["metadata"][stripe_integration.INVOICE_METADATA_KEY] == str(invoice.id)
    assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 50000
    assert kwargs["line_items"][0]["price_data"]["currency"] == "aud"


# --- webhook: signature verification ---------------------------------------


def test_webhook_rejects_invalid_signature(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            resp = client.post(
                "/webhooks/stripe", content=payload, headers={"stripe-signature": "t=123,v1=deadbeef"}
            )
            assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_webhook_returns_503_when_not_configured(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": ""}):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": "t=1,v1=x"})
            assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()


# --- webhook: reconciliation + idempotency ---------------------------------


def test_webhook_records_payment_and_marks_invoice_paid(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id, amount_total=50000)
    signature = _sign(payload)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": signature})
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    summary = get_payment_summary(db, invoice)
    assert summary["is_fully_paid"] is True
    assert summary["payments"][0].method == PaymentMethod.card
    assert summary["payments"][0].reference == "pi_test_123"


def test_webhook_is_idempotent_against_redelivery(db, booking):
    """Stripe explicitly documents the same event can be delivered more
    than once. A redelivered webhook must not double-record the payment."""
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id, amount_total=50000, payment_intent_id="pi_dedup_test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            r1 = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            r2 = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            assert r1.status_code == 200
            assert r2.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert get_total_paid(db, invoice.id) == Decimal("500.00")  # not 1000.00
    summary = get_payment_summary(db, invoice)
    assert len(summary["payments"]) == 1


def test_webhook_unknown_invoice_id_does_not_crash(db):
    payload = _checkout_completed_event(invoice_id=uuid.uuid4())

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_webhook_malformed_invoice_id_does_not_crash(db):
    event = {
        "id": "evt_test_2",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_2",
                "object": "checkout.session",
                "payment_intent": "pi_test_malformed",
                "amount_total": 50000,
                "metadata": {stripe_integration.INVOICE_METADATA_KEY: "not-a-uuid"},
            }
        },
    }
    payload = json.dumps(event).encode()

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_webhook_ignores_unrelated_event_types(db, booking):
    event = {"id": "evt_test_3", "object": "event", "type": "customer.created", "data": {"object": {"object": "customer"}}}
    payload = json.dumps(event).encode()

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


# --- invoice page: graceful fallback on Stripe errors -----------------------


class _FakeLink:
    url = "https://checkout.stripe.com/fake-link"
    id = "plink_fake123"


def test_invoice_view_offers_a_card_link_and_names_no_surcharge(db, booking):
    """The card surcharge was removed on 2026-09-11, so the page must offer
    the link and say nothing about a fee. This used to branch on whether the
    surcharge still legally applied; there is no longer a branch."""
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_fake"}), patch.object(
            stripe_integration.stripe.PaymentLink, "create", return_value=_FakeLink()
        ):
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        # the working card link replaces "contact us", on web and PDF alike
        assert "https://checkout.stripe.com/fake-link" in resp.text
        assert "Card payment is available on request" not in resp.text
        # No fee is charged, so none may be disclosed, and no wording about
        # one may survive anywhere on the page.
        assert "surcharge" not in resp.text.lower()
        assert "1.8" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_invoice_pdf_carries_the_working_card_link(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")

    captured = {}

    def _fake_pdf(html):
        captured["html"] = html
        return b"%PDF-1.4 fake"

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_fake"}), patch.object(
            stripe_integration.stripe.PaymentLink, "create", return_value=_FakeLink()
        ), patch("app.api.invoices.render_html_to_pdf", side_effect=_fake_pdf):
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        # the PDF the client receives carries the same clickable card link as
        # the web invoice, not "contact us"
        assert "https://checkout.stripe.com/fake-link" in captured["html"]
        assert "Card payment is available on request" not in captured["html"]
    finally:
        app.dependency_overrides.clear()


def test_invoice_page_falls_back_gracefully_on_stripe_error(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_fake"}), patch.object(
            stripe_integration.stripe.PaymentLink, "create", side_effect=stripe_sdk.APIConnectionError("boom")
        ):
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}")
            assert resp.status_code == 200
            assert "Card payment is available on request" in resp.text
    finally:
        app.dependency_overrides.clear()


# --- the credential belongs to the venue it was resolved for ---------------
#
# The review's proven failure: a mis-keyed link is minted inside the WRONG
# company's Stripe account; that account signs its own completion event and
# posts it to its own correctly-configured endpoint, so verification PASSES;
# the handler finds the invoice by id and records a successful payment. Money
# in the wrong company, system reports success. These are the guards.


def _deposit(db, booking):
    return create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )


def test_the_key_comes_from_the_variable_the_venue_names(db, booking, hamilton):
    """Not a key stored in the database -- the NAME of the environment
    variable holding it."""
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_PROBE"
    db.flush()
    invoice = _deposit(db, booking)

    class FakeLink:
        url = "https://checkout.stripe.com/probe"
        id = "plink_probe"

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY_PROBE": "sk_test_probe", "STRIPE_SECRET_KEY": "sk_test_wrong"}):
        with patch.object(stripe_integration.stripe.PaymentLink, "create", return_value=FakeLink()) as mock_create:
            stripe_integration.create_payment_link(invoice, Decimal("500.00"))

    assert mock_create.call_args.kwargs["api_key"] == "sk_test_probe", "it used the wrong venue's key"


def test_an_unset_variable_refuses_rather_than_falling_back(db, booking, hamilton):
    """No fallback to STRIPE_SECRET_KEY. Falling back would mint the link in
    Hamilton's account for whichever venue asked, which is the exact failure
    this exists to stop."""
    hamilton.stripe_secret_key_env = "STRIPE_SECRET_KEY_NOT_SET"
    db.flush()
    invoice = _deposit(db, booking)

    # BOTH have to be set for this to prove anything. A mutation adding
    # "or STRIPE_SECRET_KEY" survived the first version of this test,
    # because the module global is bound at import and is empty in the test
    # environment -- so the fallback had nothing to fall back TO and the
    # test passed either way.
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_hamiltons"}):
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_hamiltons"}):
            with pytest.raises(stripe_integration.StripeNotConfigured, match="STRIPE_SECRET_KEY_NOT_SET"):
                stripe_integration.create_payment_link(invoice, Decimal("500.00"))


def _real_account(account_id: str):
    """What stripe.Account.retrieve ACTUALLY returns -- a StripeObject, which
    in stripe 15.4.0 is NOT a dict subclass and has no .get().

    A plain dict stand-in here let `account.get("id")` ship: it passed 7/7
    mutation checks over a function that could not succeed against a real
    response, and would have 500'd the first invoice page after anyone armed
    the guard (review, 2026-09-12). Build the real shape, always.
    """
    return stripe_integration.stripe.Account.construct_from(
        {"id": account_id, "object": "account"}, "sk_test"
    )


def test_a_key_belonging_to_another_account_is_refused(db, booking, hamilton):
    """THE guard. The venue says which Stripe account it banks into; if the
    resolved key belongs to a different one, no link is created at all."""
    hamilton.stripe_account_id = "acct_hamilton"
    db.flush()
    invoice = _deposit(db, booking)

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_someone_elses"}):
        with patch.object(stripe_integration.stripe.Account, "retrieve", return_value=_real_account("acct_SOMEONE_ELSE")):
            with patch.object(stripe_integration.stripe.PaymentLink, "create") as mock_create:
                with pytest.raises(stripe_integration.StripeVenueMismatch, match="acct_SOMEONE_ELSE"):
                    stripe_integration.create_payment_link(invoice, Decimal("500.00"))
    assert mock_create.call_count == 0, "no link may be created at all"


def test_a_matching_account_is_allowed_through(db, booking, hamilton):
    """The guard must not refuse everything."""
    hamilton.stripe_account_id = "acct_hamilton"
    db.flush()
    invoice = _deposit(db, booking)

    class FakeLink:
        url = "https://checkout.stripe.com/ok"
        id = "plink_ok"

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_hamiltons"}):
        with patch.object(stripe_integration.stripe.Account, "retrieve", return_value=_real_account("acct_hamilton")):
            with patch.object(stripe_integration.stripe.PaymentLink, "create", return_value=FakeLink()):
                url, link_id, account = stripe_integration.create_payment_link(invoice, Decimal("500.00"))

    assert url.endswith("/ok")
    assert account == "acct_hamilton", "the link records the account that minted it"


def test_a_client_is_shown_no_card_link_when_the_account_does_not_match(db, booking, hamilton):
    """What the CLIENT gets. A refusal must degrade to 'no card option',
    never to a link into the wrong account and never to a broken page."""
    hamilton.stripe_account_id = "acct_hamilton"
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_someone_elses"}):
            with patch.object(stripe_integration.stripe.Account, "retrieve", return_value=_real_account("acct_OTHER")):
                resp = TestClient(app).get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200, "a refusal must not take the invoice page down"
        assert "checkout.stripe.com" not in resp.text, "no payment link may be offered"
    finally:
        app.dependency_overrides.clear()


# --- the per-venue webhook path, beside the old one ------------------------
#
# Aaron, 2026-09-12: "keep the current webhook path working while the
# per-venue path goes in. Live payments run through it every day and an
# interrupted webhook means a client pays and the system doesn't know."


def test_the_original_webhook_path_still_records_a_payment(db, booking):
    """The one Stripe's dashboard points at today. If this ever fails, a
    client pays and the system never hears about it."""
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is True


def test_the_per_venue_path_records_a_payment_for_its_own_venue(db, booking, hamilton):
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_PROBE"
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET_PROBE": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe/hamilton", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is True


def test_a_payment_arriving_on_the_wrong_venues_endpoint_is_not_recorded(db, booking, hamilton):
    """THE assertion. The path says which account the money went into; the
    invoice says which venue it belongs to. If they differ, the money is in
    the wrong company's account and recording it would report a paid invoice
    that has not been paid."""
    from decimal import Decimal as _D

    from app.models import Space, Venue

    other = Venue(
        name="Meantime The Entrance", slug="entrance",
        stripe_webhook_secret_env="STRIPE_WEBHOOK_SECRET_ENTRANCE",
    )
    db.add(other)
    db.flush()
    db.add(Space(
        venue_id=other.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=_D("1000"), is_bookable=True,
    ))
    db.flush()

    invoice = _deposit(db, booking)          # Hamilton's booking
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET_ENTRANCE": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe/entrance", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        # 200, because Stripe must not retry forever on something a human
        # has to unpick -- but nothing is recorded.
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is False, "the payment must NOT be recorded"

    from app.models import BookingEvent

    flagged = [
        e for e in db.query(BookingEvent).filter_by(booking_id=invoice.booking_id).all()
        if e.event_type == "payment_venue_mismatch"
    ]
    assert len(flagged) == 1, "a client HAS paid -- silence is the one unacceptable answer"


def test_an_unknown_venue_in_the_path_is_refused(db, booking):
    invoice = _deposit(db, booking)
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        # The process-wide secret must be SET for this to prove anything.
        # Without it, a mutation making an unknown venue fall back to that
        # secret has nothing to fall back to, and the 503 arrives for the
        # ordinary "not configured" reason instead -- the exact shape Aaron
        # named on 2026-09-12 and the third time tonight.
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe/not-a-venue", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 503, "an unknown venue must not borrow another venue's secret"
    finally:
        app.dependency_overrides.clear()


def test_a_venue_that_has_not_named_its_secret_does_not_borrow_hamiltons(db, hamilton):
    """The fallback that had to go. The Entrance's account signs with its own
    secret; if its row is missing the variable name and the path quietly used
    the process-wide one, every real event would fail verification, 400, be
    retried for three days and then dropped -- a client charged and an invoice
    that still says unpaid.

    A venue with no named variable must refuse, not borrow.
    """
    from app.api import webhooks

    hamilton.stripe_webhook_secret_env = None
    db.flush()

    with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_hamiltons"}):
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_hamiltons"}):
            secret, venue = webhooks._signing_secret_for(db, "hamilton")

    assert secret is None, "the venue borrowed another venue's signing secret"
    assert venue is hamilton, "the venue itself was still found -- only its secret is missing"


def test_a_venue_that_names_its_own_secret_gets_it(db, hamilton):
    """The other half: the no-fallback rule must not refuse everything."""
    from app.api import webhooks

    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_HAMILTON"
    db.flush()

    with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET_HAMILTON": "whsec_its_own"}):
        secret, venue = webhooks._signing_secret_for(db, "hamilton")

    assert secret == "whsec_its_own"
    assert venue is hamilton


def test_the_legacy_path_still_uses_the_process_wide_secret(db):
    """Aaron, 2026-09-12: parallel paths. The no-fallback rule applies to the
    per-venue path only -- the path Stripe's dashboard points at today must
    keep resolving exactly the secret it resolves now."""
    from app.api import webhooks

    with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_the_live_one"}):
        secret, venue = webhooks._signing_secret_for(db, None)

    assert secret == "whsec_the_live_one"
    assert venue is None, "the legacy path asserts no venue, as it always has"


def test_the_shared_path_refuses_an_invoice_whose_venue_signs_elsewhere(db, booking, hamilton):
    """THE UNGUARDED PATH. The venue assertion read `if venue is not None`,
    and the shared endpoint -- the one every live payment arrives at today
    -- passes None. So on the only path in use, it did nothing.

    What the shared path CAN know: one Stripe account signs with the
    process-wide secret, and each venue's row names the variable its own
    account signs with. A venue naming a DIFFERENT variable from the one
    the shared path verifies against signs with a different account, so an
    event for one of its invoices arriving here was signed by the OTHER
    company. The money is in the wrong place, and recording it would say
    the invoice was paid.
    """
    hamilton.stripe_webhook_secret_env = "STRIPE_WEBHOOK_SECRET_HAMILTON_MOVED"
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is False, (
        "a payment signed by a different company's account was recorded as paid"
    )

    from app.models import BookingEvent

    flagged = [
        e for e in db.query(BookingEvent).filter_by(booking_id=invoice.booking_id).all()
        if e.event_type == "payment_venue_mismatch"
    ]
    assert len(flagged) == 1
    assert flagged[0].old_value == "shared endpoint"


def test_the_shared_path_still_records_for_the_venue_the_shared_secret_belongs_to(db, booking, hamilton):
    """Today's behaviour, and the positive control on the test above: a
    guard that refused everything on the shared path would take live
    payments down, and the refusal above would pass for the wrong reason.

    Hamilton's row names the SHARED variable by name -- that is what seed.py
    and migration f2a9d5c81b64 write -- and that is the fact that says its
    account is the one signing here. The first draft of the guard tested
    "has the venue named anything", which refused this exact case.
    """
    from app.services.stripe_integration import DEFAULT_STRIPE_WEBHOOK_SECRET_ENV

    hamilton.stripe_webhook_secret_env = DEFAULT_STRIPE_WEBHOOK_SECRET_ENV
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is True


def test_the_shared_path_refuses_an_invoice_whose_venue_names_no_secret(db, booking, hamilton):
    """A venue with no signing-secret variable has never been wired to any
    Stripe account, so nothing it is owed can legitimately have been
    collected by the account that signs on the shared endpoint. Refused and
    flagged, not silently recorded against the wrong company."""
    hamilton.stripe_webhook_secret_env = None
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}):
            resp = TestClient(app).post(
                "/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)}
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is False

    from app.models import BookingEvent

    flagged = [
        e for e in db.query(BookingEvent).filter_by(booking_id=invoice.booking_id).all()
        if e.event_type == "payment_venue_mismatch"
    ]
    assert len(flagged) == 1


def test_the_shared_secret_is_read_live_not_captured_at_import(db):
    """It was a module constant, read once at import -- the pattern the
    module's own comment argues against for the secret key, twenty lines
    up. Rotating the secret on Railway then needed a rebuild before the
    process would see it, and a webhook arriving in that window failed
    verification, was retried by Stripe for about three days and dropped:
    a client charged, an invoice still saying unpaid.

    Asserted by changing the ENVIRONMENT and reading the answer back with
    no reimport. A module attribute cannot pass this.
    """
    from app.api import webhooks

    with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_rotated_just_now"}):
        secret, venue = webhooks._signing_secret_for(db, None)

    assert secret == "whsec_rotated_just_now"
    assert venue is None


def test_rotating_the_shared_secret_takes_effect_on_the_next_event(db, booking, hamilton):
    """End to end: an event signed with the NEW secret verifies, without
    the process being restarted."""
    from app.services.stripe_integration import DEFAULT_STRIPE_WEBHOOK_SECRET_ENV

    hamilton.stripe_webhook_secret_env = DEFAULT_STRIPE_WEBHOOK_SECRET_ENV
    db.flush()
    invoice = _deposit(db, booking)
    mark_sent(db, invoice, actor="test")
    rotated = "whsec_rotated_secret"
    payload = _checkout_completed_event(invoice_id=invoice.id)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": rotated}):
            resp = TestClient(app).post(
                "/webhooks/stripe", content=payload,
                headers={"stripe-signature": _sign(payload, rotated)},
            )
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.clear()

    assert get_payment_summary(db, invoice)["is_fully_paid"] is True
