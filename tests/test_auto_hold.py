"""A booking holds its own date once both the agreement and a deposit
invoice have been sent -- it moves itself to 'tentative', which is a real
date-holding status (BLOCKING_STATUSES + the exclusion constraint), and
stamps a hold expiry so an unpaid hold surfaces for chasing.

These tests are mostly the cases where it must behave carefully: it fires
only when BOTH are sent, never pulls a confirmed booking back, and flags
rather than double-holding a room another booking already holds.
"""

import datetime as dt

from app.models import Contact
from app.models.booking import Booking, BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.services import documents as documents_service
from app.services import invoicing
from app.services import policy
from app.services.booking import add_linked_space, change_status, create_booking
from app.services.document_generation import generate_agreement_content
from sqlalchemy import select


def _booking(db, space, *, name="Auto Hold Test", event_date=dt.date(2027, 5, 15),
             start_time=dt.time(18, 0), end_time=dt.time(23, 0)):
    contact = Contact(name="Hold Client", email=f"hold.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=start_time, end_time=end_time, event_name=name,
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


def _flag_notes(db, booking):
    return [
        e.new_value for e in booking.events
        if e.event_type == "enquiry_flagged" and e.field_name == "manual_review"
    ]


# --- the happy path, both orderings -------------------------------------------


def test_sending_both_holds_the_date_and_stamps_expiry(db, loft):
    booking = _booking(db, loft)
    _send_agreement(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry, "the agreement alone must not hold the date"

    _send_deposit_invoice(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.tentative
    assert booking.hold_expires_at == dt.date.today() + dt.timedelta(days=policy.HOLD_EXPIRY_DAYS)


def test_holds_regardless_of_send_order(db, loft):
    booking = _booking(db, loft)
    _send_deposit_invoice(db, booking)  # invoice first this time
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry

    _send_agreement(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.tentative


def test_deposit_invoice_alone_does_not_hold(db, loft):
    booking = _booking(db, loft)
    _send_deposit_invoice(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry
    assert booking.hold_expires_at is None


# --- forward-only: never pull a confirmed booking back ------------------------


def test_hold_never_pulls_a_confirmed_booking_back(db, loft):
    booking = _booking(db, loft)
    change_status(db, booking, BookingStatus.confirmed, actor="test")  # e.g. a manual, deposit-waived confirm
    _send_agreement(db, booking)
    _send_deposit_invoice(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed, "automation must never move a booking backward"


# --- linked rooms: one event, both rooms held --------------------------------


def test_hold_cascades_to_a_linked_room(db, loft, mezzanine):
    booking = _booking(db, loft)
    child = add_linked_space(db, booking, space_id=mezzanine.id, actor="test")
    _send_agreement(db, booking)
    _send_deposit_invoice(db, booking)
    db.refresh(booking)
    db.refresh(child)
    assert booking.status == BookingStatus.tentative
    assert child.status == BookingStatus.tentative, "a two-room event must hold both rooms"


# --- contention: flag, never double-hold -------------------------------------


def test_clash_flags_for_review_instead_of_double_holding(db, loft):
    held = _booking(db, loft, name="First Party", event_date=dt.date(2027, 11, 28))
    change_status(db, held, BookingStatus.tentative, actor="test")  # already holds the Loft that night

    contender = _booking(db, loft, name="Second Party", event_date=dt.date(2027, 11, 28))
    _send_agreement(db, contender)
    _send_deposit_invoice(db, contender)  # would try to hold the same room+time
    db.refresh(contender)

    assert contender.status == BookingStatus.enquiry, "must not double-hold a room another booking holds"
    notes = _flag_notes(db, contender)
    assert any("could not be held automatically" in n for n in notes)


def test_unassigned_placeholder_flags_instead_of_holding(db, unassigned_space):
    booking = _booking(db, unassigned_space)
    _send_agreement(db, booking)
    _send_deposit_invoice(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry
    assert any("no real space is assigned" in n for n in _flag_notes(db, booking))
