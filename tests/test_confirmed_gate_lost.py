"""A confirmed booking that loses a gate -- its signed agreement superseded,
or (via any future refund path) its deposit no longer paid -- is flagged
for review, never moved. Automation only walks a booking forward; the
decision to re-paper, re-collect, or cancel is a human one.
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
from app.services.booking import change_status, create_booking, flag_if_confirmed_gate_lost
from app.services.document_generation import generate_agreement_content


def _booking(db, space, *, name="Gate Test", event_date=dt.date(2027, 9, 4)):
    contact = Contact(name="Gate Client", email=f"gate.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )


def _send_and_sign_agreement(db, booking):
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    documents_service.mark_sent(db, doc, actor="test")
    return documents_service.sign(db, doc, signer_name="Gate Client", signer_ip="1.2.3.4")


def _send_and_pay_deposit(db, booking):
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")
    invoicing.record_payment(db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="test")
    return inv


def _confirmed_on_both_gates(db, space):
    b = _booking(db, space)
    _send_and_sign_agreement(db, b)
    _send_and_pay_deposit(db, b)
    db.refresh(b)
    assert b.status == BookingStatus.confirmed  # auto-confirmed on signature + deposit
    return b


def _review_notes(booking):
    return [e.new_value for e in booking.events if e.event_type == "enquiry_flagged" and e.field_name == "manual_review"]


# --- the void case: a signed agreement superseded on a confirmed booking ------


def test_superseding_a_signed_agreement_flags_but_does_not_move(db, loft):
    b = _confirmed_on_both_gates(db, loft)
    documents_service.create_new_version(db, b, DocumentType.agreement, generate_agreement_content(b), actor="staff:aaron")
    db.refresh(b)
    assert b.status == BookingStatus.confirmed, "flag, never move"
    notes = _review_notes(b)
    assert any("no longer has a signed current agreement" in n and "NOT been moved" in n for n in notes)


def test_no_flag_when_the_booking_was_not_confirmed(db, loft):
    b = _booking(db, loft)
    _send_and_sign_agreement(db, b)  # signed but unpaid -> tentative, nothing to protect
    db.refresh(b)
    assert b.status != BookingStatus.confirmed
    documents_service.create_new_version(db, b, DocumentType.agreement, generate_agreement_content(b), actor="test")
    assert _review_notes(b) == []


def test_first_agreement_on_a_confirmed_booking_does_not_flag(db, loft):
    b = _booking(db, loft)
    change_status(db, b, BookingStatus.confirmed, actor="test")  # e.g. a hand-confirmed, deposit-waived booking
    documents_service.create_new_version(db, b, DocumentType.agreement, generate_agreement_content(b), actor="test")
    assert _review_notes(b) == []  # nothing signed was voided


def test_regenerating_over_an_unsigned_agreement_does_not_flag(db, loft):
    b = _confirmed_on_both_gates(db, loft)
    documents_service.create_new_version(db, b, DocumentType.agreement, generate_agreement_content(b), actor="test")
    flagged_once = len(_review_notes(b))
    # the new current version is an unsigned draft; regenerating over THAT voids no signature
    documents_service.create_new_version(db, b, DocumentType.agreement, generate_agreement_content(b), actor="test")
    assert len(_review_notes(b)) == flagged_once


# --- the reusable deposit branch (for a future refund path) -------------------


def test_helper_flags_a_confirmed_booking_with_no_paid_deposit(db, loft):
    b = _booking(db, loft)
    _send_and_sign_agreement(db, b)
    change_status(db, b, BookingStatus.confirmed, actor="test")  # confirmed by hand, deposit never paid
    assert flag_if_confirmed_gate_lost(db, b, actor="test") is True
    db.refresh(b)
    assert b.status == BookingStatus.confirmed
    assert any("no longer has a paid deposit" in n for n in _review_notes(b))


def test_helper_is_quiet_when_both_gates_hold(db, loft):
    b = _confirmed_on_both_gates(db, loft)
    before = len(_review_notes(b))
    assert flag_if_confirmed_gate_lost(db, b, actor="test") is False
    assert len(_review_notes(b)) == before
