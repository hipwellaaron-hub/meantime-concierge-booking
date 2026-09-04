import datetime as dt
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
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", None):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.not_configured


def test_mode_test_for_sk_test_key():
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_51AbCdEf1234567890"):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.test


def test_mode_test_for_restricted_test_key():
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "rk_test_51AbCdEf1234567890"):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.test


def test_mode_live_for_sk_live_key():
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_live_51AbCdEf1234567890"):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


def test_mode_live_for_restricted_live_key():
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "rk_live_51AbCdEf1234567890"):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


def test_mode_defaults_to_live_for_unrecognized_key_shape():
    """An unrecognized shape must fail toward "assume this is real money",
    never toward "assume it's safe" -- see get_mode's own docstring."""
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "some_future_key_format_stripe_hasnt_shipped_yet"):
        assert stripe_integration.get_mode() == stripe_integration.StripeMode.live


# --- create_payment_link ---------------------------------------------------


def test_create_payment_link_raises_when_not_configured(db, booking):
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", None):
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

    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"):
        with patch.object(stripe_integration.stripe.PaymentLink, "create", return_value=FakeLink()) as mock_create:
            url, link_id = stripe_integration.create_payment_link(invoice, Decimal("500.00"))

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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", None):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
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
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)})
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


# --- invoice page: graceful fallback on Stripe errors -----------------------


class _FakeLink:
    url = "https://checkout.stripe.com/fake-link"
    id = "plink_fake123"


def test_invoice_view_names_the_card_surcharge_when_it_applies(db, booking):
    from app.services.policy import is_card_surcharge_permitted

    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 9, 1), actor="test",
    )
    mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"), patch.object(
            stripe_integration.stripe.PaymentLink, "create", return_value=_FakeLink()
        ):
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        # the working card link replaces "contact us", on web and PDF alike
        assert "https://checkout.stripe.com/fake-link" in resp.text
        assert "Card payment is available on request" not in resp.text
        # the surcharge is named explicitly while it legally applies, and the
        # line drops cleanly once the surcharge ban date passes
        if is_card_surcharge_permitted(dt.date.today()):
            assert "includes 1.8% card surcharge" in resp.text
        else:
            assert "card surcharge" not in resp.text
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
        with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"), patch.object(
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
        with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"), patch.object(
            stripe_integration.stripe.PaymentLink, "create", side_effect=stripe_sdk.APIConnectionError("boom")
        ):
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}")
            assert resp.status_code == 200
            assert "Card payment is available on request" in resp.text
    finally:
        app.dependency_overrides.clear()
