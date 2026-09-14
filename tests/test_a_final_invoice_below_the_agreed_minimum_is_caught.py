"""Two standing checks for two figures nothing was re-asking.

THE AGREED MINIMUM FOOD SPEND is compared against the order exactly once,
in the wizard's food step, shown to the client, and thrown away. Nothing at
invoice time and nothing nightly asks again, so a final invoice can go out
short of the figure the agreement's Minimum Spend clause names and nobody
finds out until after the event. Reported, never rewritten: a shortfall may
be a deliberate commercial decision, but it should be one somebody made on
purpose, before the night.

THE MIGRATED PRICING LOCK. The importer set pricing_locked_at from the
CSV's Opportunity Created date and, where the row carried none, let it
default to the import day -- and printed a warning in that run's output,
which is gone. A pre-cutover pizza booking then prices at the current rate.
Reported so it reaches the digest; a person setting the real date clears
it on the next run.

EVERY PROBE FORCES THE CONDITION. A booking's agreed minimum defaults to the
space's, and the fixtures here set it explicitly above what the invoice
charges; a migrated fixture sets pricing_locked_at to the import day by
hand rather than trusting an importer that is not under test.
"""
import ast
import datetime as dt
import inspect
from decimal import Decimal

from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing, reconciliation
from app.services.booking import create_booking


def _booking(db, loft, contact, name, *, minimum="1500.00"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    b.agreed_min_food_spend = Decimal(minimum)
    db.flush()
    return b


def _final(db, booking, charged, *, send=True, extra_lines=()):
    inv = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": charged}, *extra_lines],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    if send:
        invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
        db.flush()
    return inv


def _codes(findings):
    return [f.check_code for f in findings]


# --- the minimum spend ----------------------------------------------------------


def test_a_final_invoice_short_of_the_agreed_minimum_is_flagged(db, hamilton, loft, contact):
    """THE one. Agreed $1,500; invoiced $1,000."""
    booking = _booking(db, loft, contact, "ZZMIN Short")
    _final(db, booking, "1000.00")

    findings = reconciliation.check_final_invoice_below_minimum_spend(db, [booking])

    assert _codes(findings) == ["FINAL_INVOICE_BELOW_MINIMUM_SPEND"]
    assert "$500.00 short" in findings[0].detail


def test_an_invoice_meeting_the_minimum_is_silent(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZMIN Met")
    _final(db, booking, "1500.00")

    assert reconciliation.check_final_invoice_below_minimum_spend(db, [booking]) == []


def test_the_deposit_credit_does_not_count_as_a_shortfall(db, hamilton, loft, contact):
    """The minimum is about what was ORDERED. A $1,500 order with a $500
    deposit credited totals $1,000 on the invoice and is not short."""
    booking = _booking(db, loft, contact, "ZZMIN Credit")
    dep = invoicing.create_deposit_invoice(db, booking, due_date=dt.date.today(), actor="test")
    db.flush()
    invoicing.mark_sent(db, dep, actor="staff:test@meantime.com.au")
    invoicing.record_payment(db, dep, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")
    final = _final(db, booking, "1500.00")
    assert final.total == Decimal("1000.00"), "fixture: the credit must be on the invoice"

    assert reconciliation.check_final_invoice_below_minimum_spend(db, [booking]) == []


def test_a_discount_line_does_count(db, hamilton, loft, contact):
    """A negative charge line is a commercial decision and it lowers what
    was charged. $1,500 less a $600 loyalty discount is $900 against
    $1,500."""
    booking = _booking(db, loft, contact, "ZZMIN Discount")
    _final(db, booking, "1500.00", extra_lines=[{"description": "Loyalty discount", "quantity": 1, "unit_price": "-600.00"}])

    findings = reconciliation.check_final_invoice_below_minimum_spend(db, [booking])

    assert _codes(findings) == ["FINAL_INVOICE_BELOW_MINIMUM_SPEND"]


def test_a_draft_final_is_checked_too(db, hamilton, loft, contact):
    """Catching it before it goes out is the whole point."""
    booking = _booking(db, loft, contact, "ZZMIN Draft")
    _final(db, booking, "1000.00", send=False)

    assert _codes(reconciliation.check_final_invoice_below_minimum_spend(db, [booking])) == [
        "FINAL_INVOICE_BELOW_MINIMUM_SPEND"
    ]


def test_a_paid_final_is_left_alone(db, hamilton, loft, contact):
    """Settled. Whatever the shortfall was, it is history now, and flagging
    a receipt would sit on Triage forever."""
    booking = _booking(db, loft, contact, "ZZMIN Paid")
    final = _final(db, booking, "1000.00")
    invoicing.record_payment(db, final, amount=Decimal("1000.00"), method=PaymentMethod.bank_transfer, actor="test")
    assert final.status == InvoiceStatus.paid

    assert reconciliation.check_final_invoice_below_minimum_spend(db, [booking]) == []


def test_a_booking_with_no_minimum_is_silent(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZMIN None", minimum="0.00")
    _final(db, booking, "10.00")

    assert reconciliation.check_final_invoice_below_minimum_spend(db, [booking]) == []


# --- the migrated pricing lock ----------------------------------------------------


def test_a_migrated_booking_locked_to_the_import_day_is_flagged(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZLOCK Defaulted")
    booking.migration_source = "ivvy"
    booking.pricing_locked_at = booking.created_at.date()
    db.flush()

    findings = reconciliation.check_migrated_pricing_lock_defaulted([booking])

    assert _codes(findings) == ["PRICING_LOCK_DEFAULTED"]


def test_a_migrated_booking_with_a_real_date_is_silent(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZLOCK Real")
    booking.migration_source = "ivvy"
    booking.pricing_locked_at = booking.created_at.date() - dt.timedelta(days=200)
    db.flush()

    assert reconciliation.check_migrated_pricing_lock_defaulted([booking]) == []


def test_a_native_booking_is_never_flagged(db, hamilton, loft, contact):
    """A booking made in Concierge is locked on the day it was made. That
    is correct, not defaulted."""
    booking = _booking(db, loft, contact, "ZZLOCK Native")
    assert booking.migration_source is None
    booking.pricing_locked_at = booking.created_at.date()
    db.flush()

    assert reconciliation.check_migrated_pricing_lock_defaulted([booking]) == []


# --- registered, not merely defined ----------------------------------------------


def test_both_checks_are_wired_into_collect():
    """A check that exists and is never called is a safeguard that is
    really a log. Read from collect()'s own source, by name."""
    tree = ast.parse(inspect.getsource(reconciliation.collect))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "check_final_invoice_below_minimum_spend" in called
    assert "check_migrated_pricing_lock_defaulted" in called
