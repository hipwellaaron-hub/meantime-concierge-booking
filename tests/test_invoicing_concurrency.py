"""Proves the split-payment race is closed: two payments landing on the
same invoice at nearly the same moment must still correctly flip the
invoice to 'paid' once the combined total covers it. Before the fix, both
concurrent calls could compute total_paid from a snapshot that didn't yet
include the other's (uncommitted) payment, and neither would cross the
threshold -- the invoice would stay stuck as unpaid forever despite
genuinely being paid in full.
"""

import datetime as dt
import threading
import uuid
from decimal import Decimal

from app.models import Contact, Space, Venue
from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.services.booking import create_booking
from app.services.invoicing import create_invoice, get_by_token, get_total_paid, mark_sent, record_payment
from app.models.invoice import InvoiceType
from tests.conftest import TestSessionLocal


def test_concurrent_split_payments_correctly_flip_invoice_to_paid():
    setup = TestSessionLocal()
    venue = Venue(name="Payment Concurrency Test Venue", slug=f"payment-concurrency-test-{uuid.uuid4().hex[:8]}")
    space = Space(
        venue=venue,
        name="Test Space",
        capacity=100,
        min_food_spend=0,
        standard_min_adults=0,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    )
    contact = Contact(name="Payment Concurrency Test Contact", email="payment-concurrency@example.com")
    setup.add_all([venue, space, contact])
    setup.commit()

    booking = create_booking(
        setup,
        space_id=space.id,
        contact_id=contact.id,
        event_date=dt.date(2027, 3, 6),
        start_time=dt.time(12, 0),
        end_time=dt.time(17, 0),
        event_name="Payment Concurrency Test",
        event_type="party",
        adult_count=10,
        child_count=0,
        notes=None,
        actor="test",
    )
    line_items = [{"description": "Catering", "quantity": 1, "unit_price": "1000.00"}]
    invoice = create_invoice(setup, booking, InvoiceType.final, line_items, dt.date(2027, 3, 1), actor="test")
    invoice = mark_sent(setup, invoice, actor="test")
    token = invoice.access_token
    setup.close()

    barrier = threading.Barrier(2)
    results = {}

    def attempt(key: str, payer: str):
        session = TestSessionLocal()
        try:
            inv = get_by_token(session, token)
            barrier.wait(timeout=5)
            record_payment(
                session,
                inv,
                amount=Decimal("500.00"),
                method=PaymentMethod.bank_transfer,
                payer_name=payer,
                actor="test",
            )
            results[key] = "success"
        except ValueError as exc:
            results[key] = f"failed: {exc}"
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=("1", "Org A")),
        threading.Thread(target=attempt, args=("2", "Org B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # both payments are legitimate (different payers, correctly summing to
    # the full total) -- neither should be rejected, unlike the sign/
    # double-booking races where only one contender should win
    assert results == {"1": "success", "2": "success"}, results

    verify = TestSessionLocal()
    final_invoice = get_by_token(verify, token)
    total_paid = get_total_paid(verify, final_invoice.id)
    assert total_paid == Decimal("1000.00")
    assert final_invoice.status == InvoiceStatus.paid
    assert final_invoice.paid_at is not None
    verify.close()
