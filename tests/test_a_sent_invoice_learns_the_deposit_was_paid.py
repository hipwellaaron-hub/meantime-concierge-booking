"""A final invoice that went out before the deposit was paid gets caught.

From the stale-copy register, the only entry that can bill a client twice:
the "Less: deposit credited" line is a stored copy of get_deposit_paid,
re-derived on every edit and once more at mark_sent -- and never again.

The case it cannot reach is the ordinary one. A final invoice goes out;
THEN the deposit is paid. The client is holding a bill for the full food
total, and the deposit they have since paid sits against a different
invoice -- so paying what they were asked for means paying the deposit
twice.

DETECTED, NOT SILENTLY FIXED, and that is the design decision. An issued
tax invoice is a record of what was asked for. Rewriting its total behind a
client who is holding it is not a safe automatic repair; Revise is that
action, with a person behind it. It is also the rule reconciliation states
for itself -- "reads everything, fixes nothing, raises flags".

Two surfaces, matching the overpayment guard shipped the same day: an
immediate flag when the deposit payment lands (booking banner + Triage +
now the digest), and a reconciliation check that re-derives the condition
every run rather than trusting the flag was written.
"""
import datetime as dt
from decimal import Decimal

from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.services import invoicing, reconciliation
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Late Deposit"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _sent_final(db, booking, total="1450.00"):
    inv = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": total}],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()
    return inv


def _pay_deposit(db, booking, amount="500.00"):
    dep = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=5), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, dep, actor="staff:test@meantime.com.au")
    db.flush()
    invoicing.record_payment(
        db, dep, amount=Decimal(amount), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    return dep


# --- the detector -----------------------------------------------------------


def test_a_final_sent_before_the_deposit_is_caught(db, hamilton, loft, contact):
    """THE one. Invoice out at $1,450 with no credit; deposit paid after."""
    booking = _booking(db, loft, contact, name="ZZLATE Deposit")
    final = _sent_final(db, booking)
    assert invoicing.final_invoice_missing_deposit_credit(db, booking) is None, (
        "nothing is stale before the deposit is paid"
    )

    _pay_deposit(db, booking)

    stale = invoicing.final_invoice_missing_deposit_credit(db, booking)
    assert stale is not None and stale.id == final.id
    assert final.total == Decimal("1450.00"), "the issued invoice must not be rewritten"


def test_a_final_sent_after_the_deposit_is_not_flagged(db, hamilton, loft, contact):
    """The ordinary, correct case: mark_sent already re-derived the credit,
    so there is nothing to say."""
    booking = _booking(db, loft, contact, name="ZZORDER Fine")
    _pay_deposit(db, booking)
    final = _sent_final(db, booking)

    assert Decimal(str(final.total)) == Decimal("950.00"), "mark_sent did not apply the credit"
    assert invoicing.final_invoice_missing_deposit_credit(db, booking) is None


def test_a_part_paid_final_is_left_alone(db, hamilton, loft, contact):
    """Revise refuses once a payment exists, so the balance is a
    reconciliation question rather than a credit question -- and a flag
    telling somebody to Revise something that cannot be revised is noise."""
    booking = _booking(db, loft, contact, name="ZZPARTPAID Final")
    final = _sent_final(db, booking)
    invoicing.record_payment(
        db, final, amount=Decimal("100.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    _pay_deposit(db, booking)

    assert invoicing.final_invoice_missing_deposit_credit(db, booking) is None


def test_a_draft_final_is_not_flagged(db, hamilton, loft, contact):
    """A draft is re-derived at mark_sent, so it fixes itself on the way
    out. Flagging it would be a warning about something that cannot reach
    a client."""
    booking = _booking(db, loft, contact, name="ZZDRAFT Final")
    invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": "1450.00"}],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    _pay_deposit(db, booking)

    assert invoicing.final_invoice_missing_deposit_credit(db, booking) is None


# --- the two surfaces -------------------------------------------------------


def test_paying_the_deposit_raises_a_flag_a_human_sees(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZLATE Flagged")
    _sent_final(db, booking)
    _pay_deposit(db, booking)

    flags = [
        e for e in booking.events
        if e.event_type == "enquiry_flagged" and "pay the deposit twice" in (e.new_value or "")
    ]
    assert flags, "the client would be billed twice and nothing said so"


def test_reconciliation_finds_it_too(db, hamilton, loft, contact):
    """The backstop, which also catches invoices already in this state
    before the flag existed."""
    booking = _booking(db, loft, contact, name="ZZLATE Recon")
    _sent_final(db, booking)
    _pay_deposit(db, booking)

    findings = reconciliation.check_final_invoice_deposit_credit(db, [booking])

    assert [f.check_code for f in findings] == ["FINAL_INVOICE_STALE_DEPOSIT_CREDIT"]
    assert "500.00" in findings[0].detail and "twice" in findings[0].detail
