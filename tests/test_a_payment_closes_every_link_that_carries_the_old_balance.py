"""Every Stripe Payment Link carries the balance as at the moment it was
minted, and stays payable until something closes it.

From the stale-copy register, wrong-amount-billed and it reaches a client.
Links were closed on cancellation and on FULL payment only. Two sequences
left a chargeable link at a figure that was no longer owed:

  * A PART PAYMENT. $200 of $500 arrives by transfer; the link in the
    client's inbox still charges $500. The overpayment guard then records
    the $500 and raises a flag -- money taken, refund to arrange -- which
    is the guard working, not the link being right.

  * A DEPOSIT LANDING AGAINST THE DEPOSIT INVOICE while a final invoice is
    already out. The final's links froze its balance BEFORE the deposit
    existed, so an old link charges the deposit a second time, and the
    overpayment guard cannot see it because the amount equals the final's
    total exactly.

The fix is the one the codebase already had for full payment: close the
links, and let the next page view mint a fresh one at what is payable now
(app.api.invoices._build_invoice_context, payable_now). A client who taps
a closed link gets Stripe's inactive-link page and reopens the invoice --
friction, not a loss.

THIS REVERSES test_a_part_payment_leaves_the_links_alone (2026-09-04),
whose premise was that draining on a part payment "would strand a client
mid-way through a split payment with no way to pay the rest". That was
never true: a fresh link is minted on every invoice-page view and every
PDF download, so the client always has a way to pay the rest -- at the
right figure. The overpayment guard's own comment describes the harm the
old links cause. Reversed with the reason written down, not quietly.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing, stripe_integration
from app.services.booking import create_booking


def _booking(db, loft, contact, name):
    return create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 11, 21),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )


def _sent(db, booking, kind, total):
    invoice = invoicing.create_invoice(
        db, booking, kind, [{"description": "Line", "quantity": 1, "unit_price": total}],
        dt.date(2026, 11, 10), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    return invoice


def _link_ids(call):
    invoice = call.args[0]
    return [e.get("id") if isinstance(e, dict) else e for e in (invoice.stripe_payment_link_ids or [])]


def _closed_for(mock):
    """{invoice_id: [link ids]} across every call the mock saw."""
    out = {}
    for call in mock.call_args_list:
        out.setdefault(call.args[0].id, []).extend(_link_ids(call))
    return out


def test_a_part_payment_closes_the_links_minted_at_the_old_balance(db, hamilton, loft, contact):
    """THE reversal. $200 of $500 lands; the $500 link must not stay
    chargeable."""
    booking = _booking(db, loft, contact, "ZZLINKS Part")
    invoice = _sent(db, booking, InvoiceType.deposit, "500.00")
    invoicing.record_payment_link(db, invoice, "plink_at_500")

    with patch.object(stripe_integration, "deactivate_payment_links") as closed:
        invoicing.record_payment(
            db, invoice, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test"
        )

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent, "part-paid, still open"
    assert _closed_for(closed).get(invoice.id) == ["plink_at_500"], (
        "a link carrying the pre-payment balance was left chargeable"
    )


def test_a_full_payment_still_closes_the_links(db, hamilton, loft, contact):
    """The 2026-09-04 behaviour, unchanged."""
    booking = _booking(db, loft, contact, "ZZLINKS Full")
    invoice = _sent(db, booking, InvoiceType.deposit, "500.00")
    invoicing.record_payment_link(db, invoice, "plink_one")
    invoicing.record_payment_link(db, invoice, "plink_two")

    with patch.object(stripe_integration, "deactivate_payment_links") as closed:
        invoicing.record_payment(
            db, invoice, amount=Decimal("500.00"), method=PaymentMethod.card, reference="pi_full", actor="test"
        )

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert _closed_for(closed).get(invoice.id) == ["plink_one", "plink_two"]


def test_a_deposit_landing_closes_the_final_invoices_links(db, hamilton, loft, contact):
    """The final went out at $1,450 with no credit and the client opened
    it -- one link at $1,450. The deposit then lands on the deposit
    invoice. That $1,450 link now charges the deposit twice, and the
    overpayment guard cannot catch it because $1,450 IS the final's total."""
    booking = _booking(db, loft, contact, "ZZLINKS DepositThenFinal")
    final = _sent(db, booking, InvoiceType.final, "1450.00")
    invoicing.record_payment_link(db, final, "plink_final_at_1450")
    deposit = _sent(db, booking, InvoiceType.deposit, "500.00")

    with patch.object(stripe_integration, "deactivate_payment_links") as closed:
        invoicing.record_payment(
            db, deposit, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test"
        )

    assert _closed_for(closed).get(final.id) == ["plink_final_at_1450"], (
        "the final invoice's link still charges the full food total after the deposit landed"
    )


def test_a_deposit_landing_leaves_a_paid_final_alone(db, hamilton, loft, contact):
    """A paid final is a receipt; its links were already closed when it
    was paid, and there is nothing left to re-mint."""
    booking = _booking(db, loft, contact, "ZZLINKS PaidFinal")
    final = _sent(db, booking, InvoiceType.final, "100.00")
    invoicing.record_payment(db, final, amount=Decimal("100.00"), method=PaymentMethod.bank_transfer, actor="test")
    invoicing.record_payment_link(db, final, "plink_stale_but_paid")
    deposit = _sent(db, booking, InvoiceType.deposit, "500.00")

    with patch.object(stripe_integration, "deactivate_payment_links") as closed:
        invoicing.record_payment(
            db, deposit, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test"
        )

    assert final.id not in _closed_for(closed)


def test_a_final_payment_does_not_touch_the_deposit_invoice(db, hamilton, loft, contact):
    """The cross-invoice close runs one way. A deposit invoice's links are
    its own business and its balance did not change."""
    booking = _booking(db, loft, contact, "ZZLINKS FinalThenDeposit")
    deposit = _sent(db, booking, InvoiceType.deposit, "500.00")
    invoicing.record_payment_link(db, deposit, "plink_deposit")
    final = _sent(db, booking, InvoiceType.final, "1450.00")

    with patch.object(stripe_integration, "deactivate_payment_links") as closed:
        invoicing.record_payment(
            db, final, amount=Decimal("300.00"), method=PaymentMethod.bank_transfer, actor="test"
        )

    assert deposit.id not in _closed_for(closed)
    assert _closed_for(closed).get(final.id) == [], "the final has no links yet; nothing to close"


def test_a_stripe_failure_while_closing_never_undoes_the_payment(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZLINKS StripeDown")
    invoice = _sent(db, booking, InvoiceType.deposit, "500.00")
    invoicing.record_payment_link(db, invoice, "plink_one")

    with patch.object(stripe_integration, "deactivate_payment_links", side_effect=RuntimeError("stripe down")):
        invoicing.record_payment(
            db, invoice, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test"
        )

    assert invoicing.get_total_paid(db, invoice.id) == Decimal("200.00")
