"""The overdue-invoice digest chases what is payable now.

From the stale-copy register. get_overdue_invoices computed
invoice.total - payments-on-this-invoice: the invoice's own frozen balance.
A final invoice that went out before the deposit landed carries no credit
for it, so the digest chased the full food total -- the figure that bills
the deposit twice, and the same stale copy the client's page stopped
printing on 2026-09-14. A chase that names the wrong amount is worse than
no chase: staff ring the client quoting it.

EVERY PROBE FORCES THE STALE SEQUENCE -- final sent, THEN the deposit paid.
A deposit paid before send is already credited on the invoice's lines, so
the old arithmetic would have been right for it and a digest that ignored
the new figure would pass.
"""
import datetime as dt
from decimal import Decimal

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services.booking import change_status, create_booking
from app.services.digest import get_overdue_invoices

LONG_AGO = dt.date(2020, 1, 1)
AS_OF = dt.date(2026, 1, 1)


def _booking(db, space, name):
    contact = Contact(name="Digest Chase", email=f"chase.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 3, 6),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    return booking


def _sent_final(db, booking, total="1450.00", due=LONG_AGO):
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.final, [{"description": "Food", "quantity": 1, "unit_price": total}],
        due, actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")
    return inv


def _pay_deposit(db, booking, amount="500.00"):
    dep = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2099, 1, 1), actor="test")
    invoicing.mark_sent(db, dep, actor="test")
    invoicing.record_payment(db, dep, amount=Decimal(amount), method=PaymentMethod.bank_transfer, actor="test")
    return dep


def _row_for(result, invoice):
    return next((r for r in result if r.invoice.id == invoice.id), None)


def test_the_chase_credits_a_deposit_paid_after_the_final_went_out(db, hamilton, loft):
    """THE one. Out at $1,450, no credit; $500 lands afterwards; the digest
    must say $950, not $1,450."""
    booking = _booking(db, loft, "ZZCHASE Late")
    final = _sent_final(db, booking)
    _pay_deposit(db, booking)

    row = _row_for(get_overdue_invoices(db, hamilton, as_of=AS_OF), final)

    assert row is not None
    assert row.balance_due == Decimal("950.00"), f"the digest chases {row.balance_due}, which bills the deposit twice"


def test_a_deposit_credited_before_send_is_not_credited_twice(db, hamilton, loft):
    booking = _booking(db, loft, "ZZCHASE Early")
    _pay_deposit(db, booking)
    final = _sent_final(db, booking)
    assert final.total == Decimal("950.00"), "mark_sent did not apply the credit"

    row = _row_for(get_overdue_invoices(db, hamilton, as_of=AS_OF), final)

    assert row is not None and row.balance_due == Decimal("950.00")


def test_a_final_fully_covered_by_an_uncredited_deposit_is_not_chased(db, hamilton, loft):
    """Nothing is owed. Chasing $0 -- or worse, the old full figure -- is
    exactly the phone call this exists to prevent."""
    booking = _booking(db, loft, "ZZCHASE Covered")
    final = _sent_final(db, booking, total="500.00")
    _pay_deposit(db, booking, amount="500.00")

    assert _row_for(get_overdue_invoices(db, hamilton, as_of=AS_OF), final) is None


def test_an_ordinary_overdue_deposit_invoice_is_still_chased_in_full(db, hamilton, loft):
    """The positive control: a deposit invoice credits nothing and must
    keep chasing its own balance."""
    booking = _booking(db, loft, "ZZCHASE Deposit")
    dep = invoicing.create_deposit_invoice(db, booking, due_date=LONG_AGO, actor="test")
    invoicing.mark_sent(db, dep, actor="test")

    row = _row_for(get_overdue_invoices(db, hamilton, as_of=AS_OF), dep)

    assert row is not None and row.balance_due == Decimal("500.00")
