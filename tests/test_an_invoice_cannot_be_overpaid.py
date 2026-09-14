"""An invoice stops silently absorbing more money than it is owed.

Aaron, 2026-09-14: "This is the only thing on the list that takes money off
a client with nothing failing first."

THE MECHANISM, and it needs nothing to go wrong. A Stripe Payment Link
carries a FROZEN amount -- the balance as at the moment it was minted --
and every link an invoice has ever minted stays live and payable until the
invoice is settled or cancelled. So:

    client opens their $1,450 invoice      -> link minted for $1,450
    client pays $500 by bank transfer      -> balance now $950
    client reopens the older invoice email -> that link still charges $1,450

Total received $1,950 against a $1,450 invoice. Every drain worked. Before
this, record_payment guarded legacy, cancelled and draft -- never `paid`
and never the balance -- so it recorded the second payment, left the status
at paid, and raised nothing.

TWO PATHS, TREATED DIFFERENTLY ON PURPOSE, and this is the one design
decision here worth arguing about. Refusing an overpayment outright is
right when a human typed the figure and can retype it. It is catastrophic
on the webhook: Stripe already has the money, so refusing leaves cash taken
from a client with NO record of it in Concierge -- strictly worse than the
overpayment it declined to write down, and the same failure the webhook's
own cancelled-invoice branch was rewritten for after the 2026-09-04
incident. So staff are refused; the webhook records and flags.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.services import invoicing, reconciliation
from app.services.booking import create_booking

TOTAL = Decimal("1450.00")


def _booking(db, loft, contact, name="Overpay"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=20), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _sent_invoice(db, booking, total=TOTAL):
    invoice = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": str(total)}],
        due_date=dt.date.today() + dt.timedelta(days=13),
        actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, invoice, actor="staff:test@meantime.com.au")
    db.flush()
    return invoice


def _pay(db, invoice, amount, *, ref=None, taken=False):
    return invoicing.record_payment(
        db, invoice, amount=Decimal(amount), method=PaymentMethod.card,
        reference=ref, actor="test", money_already_taken=taken,
    )


# --- the exact scenario ----------------------------------------------------


def test_the_frozen_link_scenario_is_refused_on_the_staff_path(db, hamilton, loft, contact):
    """Aaron's figures. Part-paid by transfer, then the old link's full
    amount arrives."""
    booking = _booking(db, loft, contact, name="ZZFROZEN Link")
    invoice = _sent_invoice(db, booking)

    _pay(db, invoice, "500.00", ref="bank transfer")
    db.flush()

    with pytest.raises(ValueError) as exc:
        _pay(db, invoice, "1450.00", ref="pi_stale_link")

    message = str(exc.value)
    assert "1450.00" in message and "950.00" in message, message
    assert invoicing.get_total_paid(db, invoice.id) == Decimal("500.00"), (
        "the refused payment was recorded anyway"
    )


def test_paying_the_exact_balance_is_still_allowed(db, hamilton, loft, contact):
    """The guard must not make an invoice unpayable -- the boundary is the
    balance itself, not one cent under it."""
    booking = _booking(db, loft, contact, name="ZZEXACT Balance")
    invoice = _sent_invoice(db, booking)

    _pay(db, invoice, "500.00")
    db.flush()
    _pay(db, invoice, "950.00")
    db.flush()

    db.refresh(invoice)
    assert invoicing.get_total_paid(db, invoice.id) == TOTAL
    assert invoice.status == InvoiceStatus.paid


def test_a_split_payment_under_the_balance_is_untouched(db, hamilton, loft, contact):
    """Split invoices are a supported case; the guard must not break them."""
    booking = _booking(db, loft, contact, name="ZZSPLIT Payers")
    invoice = _sent_invoice(db, booking)

    for amount in ("400.00", "400.00", "400.00"):
        _pay(db, invoice, amount)
        db.flush()

    assert invoicing.get_total_paid(db, invoice.id) == Decimal("1200.00")
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent


# --- the webhook path must NOT refuse --------------------------------------


def test_the_webhook_records_an_overpayment_rather_than_losing_it(db, hamilton, loft, contact):
    """Stripe has the money. Refusing here would mean a client paid and
    Concierge holds no record of it."""
    booking = _booking(db, loft, contact, name="ZZWEBHOOK Overpay")
    invoice = _sent_invoice(db, booking)

    _pay(db, invoice, "500.00", ref="bank transfer")
    db.flush()
    payment = _pay(db, invoice, "1450.00", ref="pi_stale_link", taken=True)
    db.flush()

    assert payment.id is not None, "the webhook path refused a payment Stripe had already taken"
    assert invoicing.get_total_paid(db, invoice.id) == Decimal("1950.00")


def test_an_overpayment_raises_something_a_human_sees(db, hamilton, loft, contact):
    """A payment_venue_mismatch event that nothing reads is not a report.
    flag_for_review puts it on the booking's banner AND Triage's flagged
    list."""
    booking = _booking(db, loft, contact, name="ZZFLAG Overpay")
    invoice = _sent_invoice(db, booking)

    _pay(db, invoice, "500.00", ref="bank transfer")
    db.flush()
    _pay(db, invoice, "1450.00", ref="pi_stale_link", taken=True)
    db.flush()

    flags = [
        e for e in booking.events
        if e.event_type == "enquiry_flagged" and "OVERPAID" in (e.new_value or "")
    ]
    assert flags, "an overpayment raised no flag a human would see"
    assert "1950.00" in flags[0].new_value and "1450.00" in flags[0].new_value


# --- and the backstop that re-derives it -----------------------------------


def test_reconciliation_finds_an_overpaid_invoice(db, hamilton, loft, contact):
    """The backstop for anything that got in before the guard existed. It
    re-derives the condition from the payments table rather than trusting
    that a flag was written at the time."""
    booking = _booking(db, loft, contact, name="ZZRECON Overpay")
    invoice = _sent_invoice(db, booking)
    _pay(db, invoice, "500.00", ref="bank transfer")
    db.flush()
    _pay(db, invoice, "1450.00", ref="pi_stale_link", taken=True)
    db.flush()

    findings = reconciliation.check_overpaid_invoices(db, [booking])

    assert [f.check_code for f in findings] == ["INVOICE_OVERPAID"]
    assert "1950.00" in findings[0].detail and "1450.00" in findings[0].detail


def test_the_database_refuses_the_same_stripe_payment_twice(db, hamilton, loft, contact):
    """The webhook's dedup is a SELECT that runs BEFORE the invoice row lock
    and is never re-checked after, so two deliveries of one Stripe event can
    both pass it. This is the constraint behind it -- and it has to be the
    DATABASE, because the race the SELECT loses is a database race."""
    from sqlalchemy.exc import IntegrityError

    from app.models import Payment

    booking = _booking(db, loft, contact, name="ZZDEDUP Stripe")
    invoice = _sent_invoice(db, booking)
    _pay(db, invoice, "500.00", ref="pi_the_same_one", taken=True)
    db.flush()

    with pytest.raises(IntegrityError):
        sp = db.begin_nested()
        db.add(Payment(
            invoice_id=invoice.id, amount=Decimal("500.00"), method=PaymentMethod.card,
            reference="pi_the_same_one", received_at=dt.datetime.now(dt.timezone.utc),
        ))
        db.flush()
    sp.rollback()


def test_staff_may_still_reuse_a_free_text_reference(db, hamilton, loft, contact):
    """The index is deliberately PARTIAL. A blanket unique index on
    reference would refuse the second booking whose legacy deposit has no
    source ref, because legacy_documents writes the constant "Legacy deposit
    PDF uploaded" -- and would stop staff writing the same note twice. Only
    Stripe PaymentIntents carry dedup meaning."""
    booking = _booking(db, loft, contact, name="ZZFREETEXT Ref")
    invoice = _sent_invoice(db, booking)

    _pay(db, invoice, "400.00", ref="bank transfer, Tuesday")
    db.flush()
    _pay(db, invoice, "400.00", ref="bank transfer, Tuesday")
    db.flush()

    assert invoicing.get_total_paid(db, invoice.id) == Decimal("800.00")


def test_a_fully_paid_invoice_is_not_reported_as_overpaid(db, hamilton, loft, contact):
    """The boundary again, from the reporting side: paid in full is not
    overpaid, and a check that cried wolf on every settled invoice would be
    turned off within a day."""
    booking = _booking(db, loft, contact, name="ZZRECON Exact")
    invoice = _sent_invoice(db, booking)
    _pay(db, invoice, str(TOTAL))
    db.flush()

    assert reconciliation.check_overpaid_invoices(db, [booking]) == []
