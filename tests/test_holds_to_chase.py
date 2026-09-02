"""A hold that never lapses isn't a hold. Tentative bookings sitting on an
issued-but-unpaid deposit past their hold expiry surface on the dashboard
as "chase or release". Never auto-released -- only made visible.
"""

import datetime as dt
from decimal import Decimal

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import change_status, create_booking, create_hold, get_holds_to_chase
from app.services.document_generation import generate_agreement_content


def _booking(db, space, *, name="Chase Test", event_date=dt.date(2027, 7, 3)):
    contact = Contact(name="Chase Client", email=f"chase.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )


def _send_agreement(db, booking):
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    return documents_service.mark_sent(db, doc, actor="test")


def _send_deposit_invoice(db, booking):
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    return invoicing.mark_sent(db, inv, actor="test")


def _auto_held(db, space, **kw):
    """A booking auto-held on send: tentative, unpaid deposit, expiry today + 7."""
    b = _booking(db, space, **kw)
    _send_agreement(db, b)
    _send_deposit_invoice(db, b)
    db.refresh(b)
    assert b.status == BookingStatus.tentative
    return b


def test_a_fresh_hold_is_not_chased_yet(db, hamilton, loft):
    b = _auto_held(db, loft)
    assert b.hold_expires_at > dt.date.today()
    assert b not in get_holds_to_chase(db, hamilton.id)


def test_an_expired_unpaid_hold_is_chased(db, hamilton, loft):
    b = _auto_held(db, loft)
    b.hold_expires_at = dt.date.today() - dt.timedelta(days=1)
    db.commit()
    assert b in get_holds_to_chase(db, hamilton.id)


def test_a_paid_deposit_is_not_a_payment_chase(db, hamilton, loft):
    b = _auto_held(db, loft)
    b.hold_expires_at = dt.date.today() - dt.timedelta(days=1)
    db.commit()
    inv = next(i for i in b.invoices if i.type == InvoiceType.deposit)
    invoicing.record_payment(db, inv, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")
    db.refresh(b)
    # deposit paid but agreement unsigned -> still tentative, but nothing to chase for payment
    assert b.status == BookingStatus.tentative
    assert b not in get_holds_to_chase(db, hamilton.id)


def test_a_partial_payment_still_chases(db, hamilton, loft):
    b = _auto_held(db, loft)
    b.hold_expires_at = dt.date.today() - dt.timedelta(days=1)
    db.commit()
    inv = next(i for i in b.invoices if i.type == InvoiceType.deposit)
    invoicing.record_payment(db, inv, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test")
    assert b in get_holds_to_chase(db, hamilton.id), "a part-payment hasn't paid the deposit"


def test_a_tentative_with_no_expiry_still_surfaces(db, hamilton, loft):
    # The bookings that predate auto-hold (e.g. Laura: signed, unpaid) have no
    # hold_expires_at at all -- they must not be invisible.
    b = _booking(db, loft)
    change_status(db, b, BookingStatus.tentative, actor="test")
    _send_deposit_invoice(db, b)  # already tentative, so auto-hold leaves expiry unset
    db.refresh(b)
    assert b.hold_expires_at is None
    assert b in get_holds_to_chase(db, hamilton.id)


def test_a_deliberate_staff_block_is_never_chased(db, hamilton, loft):
    # create_hold: no client, no deposit invoice -- a block, not a debt.
    hold = create_hold(
        db, space_id=loft.id, event_date=dt.date(2027, 8, 14), event_name="Owner block",
        hold_expires_at=dt.date.today() - dt.timedelta(days=1), actor="test",
    )
    assert hold not in get_holds_to_chase(db, hamilton.id)


def test_dashboard_shows_the_tile_and_the_booking(admin_client, db, hamilton, loft):
    b = _auto_held(db, loft, name="Overdue Hold")
    b.hold_expires_at = dt.date.today() - dt.timedelta(days=3)
    db.commit()
    page = admin_client.get("/admin/")
    assert page.status_code == 200
    assert "Holds to chase" in page.text
    assert "Overdue Hold" in page.text
    assert b.reference_code in page.text
