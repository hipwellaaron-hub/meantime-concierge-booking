import datetime as dt
from decimal import Decimal

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.payment import PaymentMethod
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import change_status, create_booking
from app.services.digest import build_digest, get_open_enquiries, get_overdue_invoices, render_digest_text
from app.services.document_generation import generate_agreement_content


def _make_booking(db, space, *, event_date=dt.date(2027, 3, 6), status=BookingStatus.enquiry, event_name="Digest Test Booking"):
    contact = Contact(name="Digest Test Contact", email="digest.test@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=event_name,
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    if status != BookingStatus.enquiry:
        change_status(db, booking, status, actor="test")
    return booking


def test_open_enquiries_lists_enquiry_status_only(db, hamilton, loft):
    open_one = _make_booking(db, loft, event_name="Still An Enquiry")
    _make_booking(db, loft, event_date=dt.date(2027, 3, 7), event_name="Already Offered", status=BookingStatus.offered)

    result = get_open_enquiries(db, hamilton)

    names = {b.event_name for b in result}
    assert "Still An Enquiry" in names
    assert "Already Offered" not in names
    assert open_one.id in {b.id for b in result}


def test_open_enquiries_clears_once_progressed(db, hamilton, loft):
    booking = _make_booking(db, loft)
    assert booking.id in {b.id for b in get_open_enquiries(db, hamilton)}

    change_status(db, booking, BookingStatus.offered, actor="test")
    assert booking.id not in {b.id for b in get_open_enquiries(db, hamilton)}


def test_overdue_invoice_detected(db, hamilton, loft):
    booking = _make_booking(db, loft, status=BookingStatus.confirmed)
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2020, 1, 1), actor="test")
    invoicing.mark_sent(db, invoice, actor="test")

    result = get_overdue_invoices(db, hamilton, as_of=dt.date(2026, 1, 1))

    assert len(result) == 1
    assert result[0].booking.id == booking.id
    assert result[0].balance_due == Decimal("500.00")


def test_draft_invoice_never_counts_as_overdue(db, hamilton, loft):
    booking = _make_booking(db, loft, status=BookingStatus.confirmed)
    invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2020, 1, 1), actor="test")
    # Never sent -- a client was never shown this invoice.

    result = get_overdue_invoices(db, hamilton, as_of=dt.date(2026, 1, 1))
    assert result == []


def test_fully_paid_invoice_never_counts_as_overdue(db, hamilton, loft):
    booking = _make_booking(db, loft, status=BookingStatus.confirmed)
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2020, 1, 1), actor="test")
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")

    result = get_overdue_invoices(db, hamilton, as_of=dt.date(2026, 1, 1))
    assert result == []


def test_invoice_not_yet_due_is_not_overdue(db, hamilton, loft):
    booking = _make_booking(db, loft, status=BookingStatus.confirmed)
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2099, 1, 1), actor="test")
    invoicing.mark_sent(db, invoice, actor="test")

    result = get_overdue_invoices(db, hamilton, as_of=dt.date(2026, 1, 1))
    assert result == []


def test_wizard_eligible_included_in_digest(db, hamilton, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2026, 1, 5), status=BookingStatus.confirmed)
    deposit = invoicing.create_deposit_invoice(db, booking, due_date=dt.date(2025, 12, 1), actor="test")
    invoicing.mark_sent(db, deposit, actor="test")
    invoicing.record_payment(db, deposit, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")

    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test",
    )
    documents_service.mark_sent(db, agreement, actor="test")
    documents_service.sign(db, agreement, signer_name="Test Client", signer_ip="127.0.0.1")

    content = build_digest(db, hamilton, as_of=dt.date(2025, 12, 25))
    assert booking.id in {b.id for b in content.wizard_eligible}


def test_digest_is_empty_when_nothing_needs_attention(db, hamilton):
    content = build_digest(db, hamilton, as_of=dt.date(2020, 1, 1))
    assert content.is_empty is True


def test_render_digest_text_all_clear():
    from app.services.digest import DigestContent

    content = DigestContent(new_enquiries=[], wizard_eligible=[], overdue_invoices=[])
    subject, body = render_digest_text(content, dashboard_base_url="https://example.test")
    assert subject == "Meantime Concierge: all clear"
    assert "Nothing needs attention" in body


def test_render_digest_text_lists_items_with_links(db, hamilton, loft):
    booking = _make_booking(db, loft, event_name="Render Test Booking")
    content = build_digest(db, hamilton, as_of=dt.date(2026, 1, 1))

    subject, body = render_digest_text(content, dashboard_base_url="https://example.test")
    assert "1 item" in subject
    assert "Render Test Booking" in body
    assert f"https://example.test/admin/bookings/{booking.id}" in body
