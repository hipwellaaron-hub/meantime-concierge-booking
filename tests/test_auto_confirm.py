"""A booking confirms itself once the client has both signed the current
agreement and paid the deposit. These tests are mostly about the cases
where it must NOT fire: a terminal booking resurrected by a late payment,
or a booking confirmed into a room it doesn't have, would each be worse
than never automating this at all.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import (
    auto_confirm_if_ready,
    change_status,
    create_booking,
    has_paid_deposit,
    has_signed_agreement,
)
from app.services.document_generation import generate_agreement_content


def _booking(db, space, *, name="Auto Confirm Test", event_date=dt.date(2027, 4, 10),
             start_time=dt.time(18, 0), end_time=dt.time(23, 0)):
    contact = Contact(name="Auto Confirm Client", email=f"auto.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=start_time, end_time=end_time, event_name=name,
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )


def _sign_agreement(db, booking):
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")
    return documents_service.sign(db, document, signer_name="Auto Confirm Client", signer_ip="1.2.3.4")


def _pay_deposit(db, booking, *, amount=Decimal("500.00"), total="500.00"):
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": total}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=amount, method=PaymentMethod.card, actor="test")
    return invoice


# --- the happy path, both orderings -------------------------------------------


def test_signing_then_paying_confirms(db, loft):
    booking = _booking(db, loft)
    _sign_agreement(db, booking)
    db.refresh(booking)
    assert booking.status != BookingStatus.confirmed, "a signature alone must not confirm"

    _pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed


def test_paying_then_signing_confirms(db, loft):
    booking = _booking(db, loft, name="Pay First")
    _pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status != BookingStatus.confirmed, "a deposit alone must not confirm"

    _sign_agreement(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed


def test_confirmation_is_recorded_in_the_audit_trail(db, loft):
    booking = _booking(db, loft, name="Audited")
    _sign_agreement(db, booking)
    _pay_deposit(db, booking)
    db.refresh(booking)

    reasons = [e.new_value for e in booking.events if e.field_name == "status_change_reason"]
    assert "deposit paid and agreement signed" in reasons


def test_a_part_paid_deposit_does_not_confirm(db, loft):
    """A split deposit only counts once the balance is actually cleared."""
    booking = _booking(db, loft, name="Split Deposit")
    _sign_agreement(db, booking)

    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")

    invoicing.record_payment(db, invoice, amount=Decimal("200.00"), method=PaymentMethod.card, actor="test")
    db.refresh(booking)
    assert booking.status != BookingStatus.confirmed

    invoicing.record_payment(db, invoice, amount=Decimal("300.00"), method=PaymentMethod.card, actor="test")
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed


# --- what must never happen ---------------------------------------------------


@pytest.mark.parametrize(
    "terminal",
    [BookingStatus.cancelled, BookingStatus.dead, BookingStatus.archived, BookingStatus.completed],
)
def test_a_terminal_booking_is_never_resurrected(db, loft, terminal):
    """37 real bookings were archived before go-live. A late payment or a
    stray signature against one of those must not turn it back into a
    confirmed booking silently holding a room."""
    booking = _booking(db, loft, name=f"Terminal {terminal.value}")
    _sign_agreement(db, booking)
    change_status(db, booking, terminal, actor="test")

    _pay_deposit(db, booking)

    db.refresh(booking)
    assert booking.status == terminal


def test_a_booking_with_no_real_space_is_flagged_not_confirmed(db, hamilton):
    """Still in the Unassigned placeholder: there's no room to confirm it
    into, and confirming would make a fake space block real time."""
    from app.services.ivvy_import import get_unassigned_space_id

    unassigned_id = get_unassigned_space_id(db, hamilton)
    from app.models import Space

    unassigned = db.get(Space, unassigned_id)
    booking = _booking(db, unassigned, name="No Space Yet")

    _sign_agreement(db, booking)
    _pay_deposit(db, booking)

    db.refresh(booking)
    assert booking.status != BookingStatus.confirmed
    flags = [e.new_value for e in booking.events if e.event_type == "enquiry_flagged"]
    assert any("no real space is assigned" in (f or "") for f in flags)


def test_regenerating_the_agreement_after_signing_does_not_confirm(db, loft):
    """Superseding a signed agreement means what the client signed no
    longer stands, so it can't be what confirms the booking."""
    booking = _booking(db, loft, name="Superseded")
    _sign_agreement(db, booking)
    documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    assert has_signed_agreement(db, booking) is False

    _pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status != BookingStatus.confirmed


def test_a_clash_leaves_the_signature_and_payment_intact(db, loft):
    """Confirming makes a booking blocking, so it can collide with one
    already holding the room. The client's signature and money must
    survive that, and a human must be told."""
    occupied = _booking(db, loft, name="Already Confirmed")
    change_status(db, occupied, BookingStatus.confirmed, actor="test")

    clashing = _booking(db, loft, name="Clashing")
    _sign_agreement(db, clashing)
    _pay_deposit(db, clashing)

    db.refresh(clashing)
    assert clashing.status != BookingStatus.confirmed
    # the signature and the payment are still real
    assert has_signed_agreement(db, clashing) is True
    assert has_paid_deposit(db, clashing) is True
    flags = [e.new_value for e in clashing.events if e.event_type == "enquiry_flagged"]
    assert any("clash" in (f or "").lower() for f in flags)


# --- linked spaces ------------------------------------------------------------


def test_confirming_a_parent_confirms_its_linked_rooms(db, loft, mezzanine):
    from app.services.booking import add_linked_space

    booking = _booking(db, loft, name="Two Rooms")
    child = add_linked_space(
        db, booking, space_id=mezzanine.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0), actor="test"
    )

    _sign_agreement(db, booking)
    _pay_deposit(db, booking)

    db.refresh(booking)
    db.refresh(child)
    assert booking.status == BookingStatus.confirmed
    assert child.status == BookingStatus.confirmed, "the second room must be held too"


# --- the rule can't drift from the wizard's version of it ---------------------


def test_predicates_agree_with_wizard_eligibility(db, loft, hamilton):
    """get_wizard_eligible_bookings expresses the same signed-and-paid
    rule as bulk subqueries. If the two ever disagree, one of them is
    wrong about whether a booking is really won."""
    from app.services.wizard import get_wizard_eligible_bookings

    # Inside the wizard's 14-day window so the only thing under test is
    # the signed/paid pair, not the date gate.
    soon = dt.date.today() + dt.timedelta(days=7)
    booking = _booking(db, loft, name="Wizard Agreement", event_date=soon)

    assert has_signed_agreement(db, booking) is False
    assert has_paid_deposit(db, booking) is False
    assert booking.id not in {b.id for b in get_wizard_eligible_bookings(db, hamilton)}

    _sign_agreement(db, booking)
    _pay_deposit(db, booking)
    db.refresh(booking)

    assert has_signed_agreement(db, booking) is True
    assert has_paid_deposit(db, booking) is True
    assert booking.id in {b.id for b in get_wizard_eligible_bookings(db, hamilton)}


def test_auto_confirm_is_a_no_op_on_an_already_confirmed_booking(db, loft):
    booking = _booking(db, loft, name="Already Done")
    _sign_agreement(db, booking)
    _pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed

    assert auto_confirm_if_ready(db, booking, actor="test") is False
