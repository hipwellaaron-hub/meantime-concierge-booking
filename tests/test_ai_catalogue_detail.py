"""Remaining read endpoints (brief sections 3.3, 3.4, 3.5).

Two of these tests are about what must NOT come back: a Stripe identifier
(section 3.6) and document content (section 6). Both are things the AI has
no need for and every reason not to hold.
"""

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Contact, MenuItem
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.menu_item import MenuItemCategory
from app.services import catalogue as catalogue_service
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content
from app.services.policy import PIZZA_LEGACY_PRICING_CUTOVER_DATE

TOKEN = "test-ai-token-do-not-use-in-production"
STRIPE_PAYMENT_INTENT = "pi_3QsecretIdentifierThatMustNeverLeak"


@pytest.fixture()
def ai_client(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client
    finally:
        app.dependency_overrides.clear()


def _booking(db, space, *, name="Detail Test"):
    contact = Contact(name="Detail Client", email="detail.client@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=40),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="staff:test",
    )


# --- 3.3 catalogue ------------------------------------------------------


def test_catalogue_returns_current_prices_by_default(ai_client, menu_items):
    body = ai_client.get("/api/ai/catalogue").json()
    assert body["count"] > 0
    assert body["pricing_as_of"] is None
    item = body["items"][0]
    assert item["price"] == item["current_price"]


def test_catalogue_as_of_resolves_legacy_pizza_pricing(ai_client, db, menu_items):
    pizza = db.query(MenuItem).filter(
        MenuItem.category == MenuItemCategory.pizza, MenuItem.legacy_price.isnot(None)
    ).first()
    if pizza is None:
        pytest.skip("no pizza with a legacy price in the seeded catalogue")

    before = (PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=1)).isoformat()
    body = ai_client.get(f"/api/ai/catalogue?as_of={before}").json()
    row = next(i for i in body["items"] if i["id"] == str(pizza.id))
    assert row["price"] == str(pizza.legacy_price)

    after = PIZZA_LEGACY_PRICING_CUTOVER_DATE.isoformat()
    body = ai_client.get(f"/api/ai/catalogue?as_of={after}").json()
    row = next(i for i in body["items"] if i["id"] == str(pizza.id))
    assert row["price"] == str(pizza.current_price)


def test_catalogue_reports_an_undefined_legacy_price_as_unknown(ai_client, db, menu_items):
    """A legacy-priced item with no legacy price must read as unknown, not
    fall back to today's price -- guessing here is how a wrong quote goes
    out."""
    orphan = MenuItem(
        category=MenuItemCategory.pizza, name="Test Pizza No Legacy",
        current_price=Decimal("26.00"), legacy_price=None, is_active=True,
    )
    db.add(orphan)
    db.commit()

    before = (PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=1)).isoformat()
    body = ai_client.get(f"/api/ai/catalogue?as_of={before}").json()
    row = next(i for i in body["items"] if i["id"] == str(orphan.id))
    assert row["price"] is None
    assert "no legacy price" in row["price_unknown_reason"]


def test_catalogue_states_that_serving_sizes_are_not_stored(ai_client, menu_items):
    """One of the original errors was describing a platter as feeding a
    fixed number of people. Nothing stores that, so the response says so."""
    body = ai_client.get("/api/ai/catalogue").json()
    assert "not stored" in body["notes"]["serving_sizes"].lower()
    for item in body["items"]:
        assert "serves" not in item


def test_date_resolver_agrees_with_the_booking_resolver(db, loft, menu_items):
    """Guards drift between resolve_price (booking-based, used by the
    wizard and documents) and resolve_price_as_of (date-based, used by the
    AI endpoint). They encode the same rule and must not diverge."""
    booking = _booking(db, loft)
    for item in db.query(MenuItem).all():
        assert catalogue_service.resolve_price(item, booking) == catalogue_service.resolve_price_as_of(
            item, booking.pricing_locked_at
        ), item.name


# --- 3.4 documents and invoices -----------------------------------------


def test_document_detail_never_returns_content_or_a_pdf_link(ai_client, db, loft):
    booking = _booking(db, loft)
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, doc, actor="staff:test")

    body = ai_client.get(f"/api/ai/bookings/{booking.id}/documents").json()
    entry = body["documents"][0]
    assert entry["type"] == "agreement"
    assert entry["status"] == "sent"
    assert entry["version"] == 1
    # The point of section 3.4: existence and state, not the contract text.
    assert "content" not in entry
    assert "access_token" not in entry
    raw = ai_client.get(f"/api/ai/bookings/{booking.id}/documents").text
    assert "Deposits" not in raw, "clause text must not be reachable"


def test_invoice_detail_never_leaks_a_stripe_identifier(ai_client, db, loft):
    """Payment.reference holds the Stripe payment_intent id for card
    payments. Section 3.6 forbids returning Stripe identifiers, so the
    field is not serialised at all."""
    from app.models.payment import PaymentMethod

    booking = _booking(db, loft)
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="staff:test",
    )
    invoicing.mark_sent(db, inv, actor="staff:test")
    invoicing.record_payment(
        db, inv, amount=Decimal("500.00"), method=PaymentMethod.card,
        reference=STRIPE_PAYMENT_INTENT, payer_name="Card Payer", actor="stripe_webhook",
    )

    resp = ai_client.get(f"/api/ai/bookings/{booking.id}/invoices")
    assert resp.status_code == 200
    assert STRIPE_PAYMENT_INTENT not in resp.text
    assert "pi_" not in resp.text

    payment = resp.json()["invoices"][0]["payments"][0]
    assert payment["amount"] == "500.00"
    assert payment["method"] == "card"
    assert payment["payer_name"] == "Card Payer"
    assert "reference" not in payment


def test_invoice_detail_reports_status_and_amounts(ai_client, db, loft):
    booking = _booking(db, loft)
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="staff:test",
    )
    invoicing.mark_sent(db, inv, actor="staff:test")

    entry = ai_client.get(f"/api/ai/bookings/{booking.id}/invoices").json()["invoices"][0]
    assert entry["type"] == "deposit"
    assert entry["status"] == "sent"
    assert entry["total"] == "500.00"
    assert entry["paid_at"] is None


# --- 3.5 audit trail ----------------------------------------------------


def test_events_endpoint_returns_the_audit_trail(ai_client, db, loft):
    booking = _booking(db, loft)
    body = ai_client.get(f"/api/ai/bookings/{booking.id}/events").json()
    assert body["count"] > 0
    event = body["events"][0]
    assert {"at", "event_type", "actor"} <= set(event)
    assert event["actor"] == "staff:test"


# --- venue scoping ------------------------------------------------------


def test_unknown_booking_is_404_not_403(ai_client):
    missing = "00000000-0000-0000-0000-000000000000"
    for suffix in ["documents", "invoices", "events"]:
        resp = ai_client.get(f"/api/ai/bookings/{missing}/{suffix}")
        assert resp.status_code == 404, suffix
