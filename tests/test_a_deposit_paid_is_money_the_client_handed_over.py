"""get_deposit_paid counts what the client paid, not what is still alive.

Flagged in the 2026-09-14 audit as an unexplained asymmetry:
get_deposit_paid selects deposit invoices with NO status filter, while its
immediate neighbour has_active_final_invoice explicitly excludes cancelled
ones. The audit called it "possibly intended" and asked for a ruling.

IT IS INTENDED, AND THE TIDY-UP WOULD BE A MONEY BUG. These tests exist to
make that expensive to undo:

  * cancel_invoice REFUSES a paid invoice, so a cancelled deposit invoice
    can only ever have been draft or sent.
  * A SENT deposit invoice can hold a PART payment -- and cancelling the
    booking cancels the invoice with that payment still on it.
  * Concierge models no refunds. Nothing in the schema can know the money
    went back, so dropping it from the count credits the client less than
    they handed over and bills them the difference.

The failure mode this guards against is not a bug someone writes. It is a
bug someone writes while making two adjacent functions consistent.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Deposit Paid"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _sent_deposit(db, booking):
    inv = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()
    return inv


# --- the ruling -------------------------------------------------------------


def test_a_part_payment_on_a_cancelled_deposit_still_counts(db, hamilton, loft, contact):
    """THE one. The client handed over $200. Cancelling the invoice does
    not hand it back, and Concierge has no way to know if anyone did."""
    booking = _booking(db, loft, contact, name="ZZCANCELLED PartPaid")
    invoice = _sent_deposit(db, booking)
    invoicing.record_payment(
        db, invoice, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    assert invoice.status == InvoiceStatus.sent, "a part payment must not settle the invoice"

    invoicing.cancel_invoice(db, invoice, actor="test")
    db.flush()

    assert invoice.status == InvoiceStatus.cancelled
    assert invoicing.get_deposit_paid(db, booking) == Decimal("200.00"), (
        "the client was not credited for money they actually paid"
    )


def test_a_paid_deposit_cannot_be_cancelled_at_all(db, hamilton, loft, contact):
    """The reason the case above is the ONLY shape this can take -- and the
    reason a cancelled deposit never hides a full payment."""
    booking = _booking(db, loft, contact, name="ZZCANCELLED FullPaid")
    invoice = _sent_deposit(db, booking)
    invoicing.record_payment(
        db, invoice, amount=invoice.total, method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    assert invoice.status == InvoiceStatus.paid

    with pytest.raises(ValueError, match="cannot cancel a paid invoice"):
        invoicing.cancel_invoice(db, invoice, actor="test")


def test_it_sums_across_several_deposit_invoices(db, hamilton, loft, contact):
    """A reissued deposit leaves the old one cancelled beside the new one.
    Both carry real money and both must count."""
    booking = _booking(db, loft, contact, name="ZZCANCELLED Reissued")
    first = _sent_deposit(db, booking)
    invoicing.record_payment(
        db, first, amount=Decimal("150.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    invoicing.cancel_invoice(db, first, actor="test")
    db.flush()

    second = _sent_deposit(db, booking)
    invoicing.record_payment(
        db, second, amount=Decimal("350.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()

    assert invoicing.get_deposit_paid(db, booking) == Decimal("500.00")


def test_the_deposit_credit_on_a_final_invoice_uses_that_figure(db, hamilton, loft, contact):
    """Where it actually lands: a client-facing document. This is why the
    "tidy-up" would be expensive rather than merely wrong."""
    booking = _booking(db, loft, contact, name="ZZCANCELLED Credit")
    invoice = _sent_deposit(db, booking)
    invoicing.record_payment(
        db, invoice, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    invoicing.cancel_invoice(db, invoice, actor="test")
    db.flush()

    final = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": "1000.00"}],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()

    credits = [li for li in final.line_items if li["description"] == invoicing.DEPOSIT_CREDIT_DESCRIPTION]
    assert credits, "the client got no credit for a deposit they part-paid"
    assert Decimal(credits[0]["unit_price"]) == Decimal("-200.00")
    assert final.total == Decimal("800.00")


def test_no_deposit_invoice_at_all_is_zero_not_an_error(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZCANCELLED None")

    assert invoicing.get_deposit_paid(db, booking) == Decimal("0.00")
