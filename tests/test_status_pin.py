"""Manual override always wins. A status set by hand (the staff dropdown,
via transition_status) pins the booking: the automatic transitions --
auto-hold on send, auto-confirm on deposit + signature -- leave it alone
until a human hands it back. Breast Cancer Trials (confirmed by hand with
the deposit waived) is the canonical case that must never be walked
anywhere by automation.
"""

import datetime as dt
import re
from decimal import Decimal

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import clear_status_pin, create_booking, transition_status
from app.services.document_generation import generate_agreement_content


def _booking(db, space, *, name="Pin Test", event_date=dt.date(2027, 6, 12)):
    contact = Contact(name="Pin Client", email=f"pin.{name.replace(' ', '.').lower()}@example.com")
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
    return documents_service.sign(db, doc, signer_name="Pin Client", signer_ip="1.2.3.4")


def _send_and_pay_deposit(db, booking):
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")
    invoicing.record_payment(db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="test")
    return inv


def _pin_events(booking):
    return [e for e in booking.events if e.field_name == "status_pinned_at"]


# --- pinning ------------------------------------------------------------------


def test_a_manual_status_change_pins_the_booking(db, loft):
    booking = _booking(db, loft)
    assert booking.status_pinned is False
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")
    db.refresh(booking)
    assert booking.status_pinned is True
    assert len(_pin_events(booking)) == 1


def test_resubmitting_the_current_status_does_not_pin(db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.enquiry, actor="staff:aaron")  # a double-submit no-op
    db.refresh(booking)
    assert booking.status_pinned is False


def test_pin_is_recorded_once_not_on_every_manual_move(db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")
    transition_status(db, booking, BookingStatus.tentative, actor="staff:aaron")
    db.refresh(booking)
    assert booking.status == BookingStatus.tentative
    assert len(_pin_events(booking)) == 1, "already under manual control -- no second pin event"


# --- automation defers to a pinned booking -----------------------------------


def test_pinned_booking_is_not_auto_held_on_send(db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")  # pinned at offered

    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    documents_service.mark_sent(db, doc, actor="test")
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")

    db.refresh(booking)
    assert booking.status == BookingStatus.offered, "auto-hold must defer to a hand-set status"


def test_pinned_booking_is_not_auto_confirmed(db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")  # pinned
    _send_and_sign_agreement(db, booking)
    _send_and_pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.offered, "auto-confirm must defer to a hand-set status"


def test_manually_confirmed_deposit_waived_booking_is_never_moved(db, loft):
    # The Breast Cancer Trials case: confirmed by hand, no deposit ever paid.
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")
    transition_status(db, booking, BookingStatus.tentative, actor="staff:aaron")
    transition_status(db, booking, BookingStatus.confirmed, actor="staff:aaron", reason="deposit waived")
    _send_and_sign_agreement(db, booking)  # a fresh agreement being sent must not knock it back
    db.refresh(booking)
    assert booking.status == BookingStatus.confirmed
    assert booking.status_pinned is True


# --- hand back to automation -------------------------------------------------


def test_handing_back_lets_automation_catch_up_immediately(db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")  # pinned
    _send_and_sign_agreement(db, booking)
    _send_and_pay_deposit(db, booking)
    db.refresh(booking)
    assert booking.status == BookingStatus.offered  # held back by the pin

    clear_status_pin(db, booking, actor="staff:aaron")
    db.refresh(booking)
    assert booking.status_pinned is False
    # everything the client already did now takes effect: held, then confirmed
    assert booking.status == BookingStatus.confirmed


def test_hand_back_route_clears_the_pin(admin_client, db, loft):
    booking = _booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="staff:aaron")
    detail = admin_client.get(f"/admin/bookings/{booking.id}")
    assert "Hand back to automation" in detail.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/status/unpin", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.expire_all()
    db.refresh(booking)
    assert booking.status_pinned is False
