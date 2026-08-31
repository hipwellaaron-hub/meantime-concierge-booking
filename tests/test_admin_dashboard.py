import datetime as dt
import re

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.invoice import InvoiceType
from app.services import invoicing
from app.services.booking import create_booking


def test_dashboard_counts_match_real_data(admin_client, db, loft):
    # One open enquiry -- counted.
    create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 5, 1), event_name="Enquiry Only",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )

    contact = Contact(name="Dashboard Test Contact", email="dashboard.test@example.com")
    db.add(contact)
    db.flush()

    # One confirmed booking with a sent (unpaid) invoice -- counted as unpaid.
    confirmed = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 2),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Confirmed With Unpaid Invoice",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test", status=BookingStatus.confirmed,
    )
    invoice = invoicing.create_invoice(
        db, confirmed, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")

    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    # Matched by label rather than position: asserting a bare list of
    # numbers meant adding a tile broke this test without anything
    # actually being wrong.
    tiles = dict(
        (label.strip(), int(number))
        for number, label in re.findall(
            r'<div class="num"[^>]*>(\d+)</div>\s*<div class="label">([^<]+)</div>', resp.text
        )
    )
    assert tiles["Open enquiries"] == 1
    assert tiles["Awaiting triage"] == 0
    assert tiles["Wizard-ready"] == 0
    assert tiles["Unpaid invoices"] == 1
    assert tiles["Enquiry emails failed"] == 0
    assert tiles["BEOs to review"] == 0


def test_dashboard_shows_recent_activity(admin_client, db, booking):
    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "created" in resp.text
    assert "test" in resp.text


# --- Stripe mode indicator ---------------------------------------------------


def test_dashboard_shows_not_configured_by_default(admin_client):
    # Tests never set STRIPE_SECRET_KEY, matching a fresh/unconfigured
    # deployment -- this is the real default, not a contrived patch.
    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "Stripe not configured" in resp.text
    assert "STRIPE TEST MODE" not in resp.text


def test_dashboard_shows_test_mode_banner(admin_client):
    from unittest.mock import patch

    from app.services import stripe_integration

    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"):
        resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "STRIPE TEST MODE" in resp.text
    assert "no real card payments are being taken" in resp.text


def test_dashboard_shows_live_badge_not_alarming_banner(admin_client):
    from unittest.mock import patch

    from app.services import stripe_integration

    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_live_fake"):
        resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "Stripe is live" in resp.text  # the green header status badge's tooltip
    assert "STRIPE TEST MODE" not in resp.text
    assert "STRIPE NOT CONFIGURED" not in resp.text


def test_triage_page_also_carries_the_banner(admin_client):
    """The banner has to be unavoidable across the whole admin surface,
    not just the dashboard -- Triage is a different router entirely."""
    from unittest.mock import patch

    from app.services import stripe_integration

    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"):
        resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "STRIPE TEST MODE" in resp.text
