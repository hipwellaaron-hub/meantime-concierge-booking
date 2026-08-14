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
    counts = [int(n) for n in re.findall(r'<div class="num"[^>]*>(\d+)</div>', resp.text)]
    # open_enquiries, triage, wizard_ready, unpaid_invoices, notification_failures
    assert counts == [1, 0, 0, 1, 0]


def test_dashboard_shows_recent_activity(admin_client, db, booking):
    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "created" in resp.text
    assert "test" in resp.text
