"""A dead booking's client-facing links must actually die (2026-09-04).

Sophie Mavridis (HAM-20261121-MABPT, offered the Loft, 21 November) still
had a working agreement-sign link and a working Stripe card link after
Chanai Duncombe confirmed the same room and night. Three things had to be
built, all hung off app.services.booking.change_status so every path that
moves a booking (the staff dropdown, auto-confirm, this new
auto-supersede) gets the same guarantees:

1. A booking moving to a terminal status cancels its own live invoices,
   which deactivates every Stripe Payment Link ever created for them.
2. A booking becoming tentative or confirmed kills any rival `offered`
   booking on the same space and overlapping time.
3. The public document and invoice routes refuse to act once the booking
   behind them is terminal (or, for documents, once a newer version has
   superseded them) -- a "no longer available" page, not a 404.
"""

import datetime as dt
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import stripe_integration
from app.services.booking import (
    add_linked_space,
    create_booking,
    times_overlap,
    transition_status,
)
from app.services.documents import create_new_version, get_by_token, mark_sent as mark_document_sent, sign
from app.services.invoicing import (
    cancel_invoice,
    create_invoice,
    mark_sent as mark_invoice_sent,
    record_payment,
    record_payment_link,
)


def _booking_on(db, space, contact, *, when=dt.date(2026, 11, 21), start=dt.time(18, 0), end=dt.time(23, 0), name="Test Event"):
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=when,
        start_time=start, end_time=end, event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )


# --- a booking dying cancels its own invoices and Stripe links --------------


def test_moving_to_dead_cancels_live_invoices_and_deactivates_their_stripe_links(db, loft, contact):
    sophie = _booking_on(db, loft, contact, name="Sophie's Party")
    invoice = create_invoice(
        db, sophie, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    record_payment_link(db, invoice, "plink_one")
    record_payment_link(db, invoice, "plink_two")  # she reloaded the page twice -- two live links

    transition_status(db, sophie, BookingStatus.offered, actor="staff:test")
    with patch.object(stripe_integration, "deactivate_payment_links") as mock_deactivate:
        transition_status(db, sophie, BookingStatus.dead, actor="staff:test")

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.cancelled
    mock_deactivate.assert_called_once_with(["plink_one", "plink_two"])


def test_a_paid_invoice_is_left_alone_when_the_booking_dies(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    invoice.status = InvoiceStatus.paid  # already settled before the booking later dies
    db.commit()

    transition_status(db, booking, BookingStatus.offered, actor="staff:test")
    transition_status(db, booking, BookingStatus.dead, actor="staff:test")

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid  # untouched -- cancel_invoice refuses a paid invoice anyway


def test_one_invoices_stripe_failure_does_not_stop_the_booking_from_dying_or_the_others_cancelling(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    first = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    second = create_invoice(
        db, booking, InvoiceType.final, [{"description": "Balance", "quantity": 1, "unit_price": "1000.00"}],
        dt.date(2026, 11, 20), actor="test",
    )
    mark_invoice_sent(db, first, actor="test")
    mark_invoice_sent(db, second, actor="test")

    transition_status(db, booking, BookingStatus.offered, actor="staff:test")
    with patch("app.services.invoicing.cancel_invoice", side_effect=[RuntimeError("stripe is down"), None]):
        result = transition_status(db, booking, BookingStatus.dead, actor="staff:test")

    assert result.status == BookingStatus.dead  # the status change itself is never blocked


# --- confirming one booking kills a rival offer on the same slot -----------


def test_confirming_a_booking_kills_a_rival_offer_on_the_same_room_and_night(db, loft, contact):
    sophie = _booking_on(db, loft, contact, name="Sophie's Party")
    transition_status(db, sophie, BookingStatus.offered, actor="staff:test")

    chanai_contact = contact.__class__(name="Chanai Duncombe", email="chanai@example.com")
    db.add(chanai_contact)
    db.flush()
    chanai = _booking_on(db, loft, chanai_contact, name="Chanai's Party")
    transition_status(db, chanai, BookingStatus.offered, actor="staff:test")
    transition_status(db, chanai, BookingStatus.tentative, actor="staff:test")
    transition_status(db, chanai, BookingStatus.confirmed, actor="staff:test")

    db.refresh(sophie)
    assert sophie.status == BookingStatus.dead
    reasons = [
        e.new_value for e in sophie.events
        if e.event_type == "status_changed" and e.field_name == "status_change_reason"
    ]
    assert any(chanai.reference_code in r for r in reasons)


def test_a_non_overlapping_offer_the_same_night_is_left_alone(db, loft, contact):
    lunch = _booking_on(db, loft, contact, name="Lunch Offer", start=dt.time(11, 0), end=dt.time(15, 0))
    transition_status(db, lunch, BookingStatus.offered, actor="staff:test")

    other = contact.__class__(name="Evening Client", email="evening@example.com")
    db.add(other)
    db.flush()
    evening = _booking_on(db, loft, other, name="Evening Event", start=dt.time(18, 0), end=dt.time(23, 0))
    transition_status(db, evening, BookingStatus.offered, actor="staff:test")
    transition_status(db, evening, BookingStatus.tentative, actor="staff:test")

    db.refresh(lunch)
    assert lunch.status == BookingStatus.offered  # never contested -- times_overlap says so
    assert not times_overlap(lunch, evening)


def test_an_offer_in_a_different_space_is_left_alone(db, loft, lounge, contact):
    lounge_offer = _booking_on(db, lounge, contact, name="Lounge Party")
    transition_status(db, lounge_offer, BookingStatus.offered, actor="staff:test")

    other = contact.__class__(name="Loft Client", email="loftclient@example.com")
    db.add(other)
    db.flush()
    loft_booking = _booking_on(db, loft, other, name="Loft Event")  # same night, same times, different room
    transition_status(db, loft_booking, BookingStatus.offered, actor="staff:test")
    transition_status(db, loft_booking, BookingStatus.tentative, actor="staff:test")

    db.refresh(lounge_offer)
    assert lounge_offer.status == BookingStatus.offered  # a different room was never a rival


def test_a_linked_child_never_runs_or_is_hit_by_the_supersede_check(db, loft, lounge, contact):
    parent = _booking_on(db, loft, contact, name="Two Room Party")
    child = add_linked_space(db, parent, space_id=lounge.id, actor="staff:test")
    transition_status(db, parent, BookingStatus.offered, actor="staff:test")
    db.refresh(child)

    rival_contact = contact.__class__(name="Rival Client", email="rival@example.com")
    db.add(rival_contact)
    db.flush()
    rival = _booking_on(db, lounge, rival_contact, name="Rival on the Lounge")
    transition_status(db, rival, BookingStatus.offered, actor="staff:test")
    # The child (parent_booking_id set) moving into tentative must not
    # attempt to supersede anything, and must not itself be a valid rival.
    transition_status(db, rival, BookingStatus.tentative, actor="staff:test")

    db.refresh(rival)
    assert rival.status == BookingStatus.tentative  # the linked child was never a contender to knock out


# --- public document routes gate on booking status and document currency ---


def test_signing_is_refused_once_the_booking_is_terminal(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    document = create_new_version(db, booking, DocumentType.agreement, {"x": 1}, actor="test")
    mark_document_sent(db, document, actor="test")
    transition_status(db, booking, BookingStatus.offered, actor="staff:test")
    transition_status(db, booking, BookingStatus.dead, actor="staff:test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}")
        assert resp.status_code == 410
        assert "no longer available" in resp.text.lower()

        resp = client.post(f"/d/{document.access_token}/sign", data={"signer_name": "Sophie Mavridis"})
        assert resp.status_code == 410
    finally:
        app.dependency_overrides.clear()

    db.refresh(document)
    assert document.signed_at is None  # the signature never happened


def test_a_superseded_document_version_is_unavailable_even_though_the_booking_lives(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    v1 = create_new_version(db, booking, DocumentType.agreement, {"v": 1}, actor="test")
    mark_document_sent(db, v1, actor="test")
    old_token = v1.access_token

    create_new_version(db, booking, DocumentType.agreement, {"v": 2}, actor="test")  # supersedes v1

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{old_token}")
        assert resp.status_code == 410
    finally:
        app.dependency_overrides.clear()


def test_signing_still_works_on_a_live_current_document(db, loft, contact):
    """The gate must not be so broad it blocks a genuine, still-live sign."""
    booking = _booking_on(db, loft, contact)
    document = create_new_version(db, booking, DocumentType.agreement, {"x": 1}, actor="test")
    mark_document_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(f"/d/{document.access_token}/sign", data={"signer_name": "Real Client"}, follow_redirects=False)
        assert resp.status_code == 303
    finally:
        app.dependency_overrides.clear()


# --- public invoice routes gate on cancelled/terminal, and stop minting links ---


def test_invoice_link_is_unavailable_once_the_booking_is_terminal_and_mints_no_new_stripe_link(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    transition_status(db, booking, BookingStatus.offered, actor="staff:test")
    transition_status(db, booking, BookingStatus.dead, actor="staff:test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"), \
             patch.object(stripe_integration.stripe.PaymentLink, "create") as mock_create:
            client = TestClient(app)
            resp = client.get(f"/i/{invoice.access_token}")
            assert resp.status_code == 410
            assert "no longer active" in resp.text.lower()

            pdf_resp = client.get(f"/i/{invoice.access_token}/pdf")
            assert pdf_resp.status_code == 410
        mock_create.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_invoice_link_is_unavailable_once_cancelled_directly(db, loft, contact):
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    cancel_invoice(db, invoice, actor="staff:test")  # booking itself stays alive -- a staff re-issue, not a dead date

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 410
    finally:
        app.dependency_overrides.clear()


# --- webhook: a payment that lands after cancellation is flagged, not lost -


def test_webhook_flags_for_review_instead_of_silently_dropping_a_payment_on_a_closed_invoice(db, loft, contact):
    import hashlib
    import hmac
    import json
    import time

    from app.services.stripe_integration import INVOICE_METADATA_KEY

    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    cancel_invoice(db, invoice, actor="staff:test")

    secret = "whsec_test_secret_for_unit_tests"
    event = {
        "id": "evt_late_1", "object": "event", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_late_1", "object": "checkout.session", "payment_intent": "pi_late_1",
            "amount_total": 50000, "metadata": {INVOICE_METADATA_KEY: str(invoice.id)},
        }},
    }
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload
    signature = f"t={timestamp},v1={hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()}"

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch("app.api.webhooks.STRIPE_WEBHOOK_SECRET", secret):
            client = TestClient(app)
            resp = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": signature})
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    db.refresh(booking)
    flags = [e.new_value for e in booking.events if e.event_type == "enquiry_flagged"]
    assert any("pi_late_1" in f and "refund" in f.lower() for f in flags)


# --- paying an invoice must also close its links (review finding, 2026-09-04) ---


def test_paying_an_invoice_in_full_deactivates_every_payment_link_ever_minted(db, loft, contact):
    """The commonest exit from an invoice is payment, not cancellation, and
    it used to drain nothing: deactivate_payment_links had exactly one
    caller (cancel_invoice). A Payment Link never expires and a fresh one
    is minted on every page view and PDF download, so a client who opened
    the invoice three times kept three chargeable links after paying --
    and could pay the same invoice again from any of them.
    """
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    record_payment_link(db, invoice, "plink_view_one")
    record_payment_link(db, invoice, "plink_view_two")
    record_payment_link(db, invoice, "plink_pdf")  # she opened it twice and downloaded the PDF

    with patch.object(stripe_integration, "deactivate_payment_links") as mock_deactivate:
        record_payment(
            db, invoice, amount=Decimal("500.00"), method=PaymentMethod.card,
            reference="pi_paid_in_full", actor="test",
        )

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    mock_deactivate.assert_called_once_with(["plink_view_one", "plink_view_two", "plink_pdf"])


def test_a_part_payment_leaves_the_links_alone(db, loft, contact):
    # Still payable, so the links must stay live -- draining on any
    # payment rather than on the settling one would strand a client
    # mid-way through a split payment with no way to pay the rest.
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    record_payment_link(db, invoice, "plink_one")

    with patch.object(stripe_integration, "deactivate_payment_links") as mock_deactivate:
        record_payment(
            db, invoice, amount=Decimal("200.00"), method=PaymentMethod.card,
            reference="pi_part", actor="test",
        )

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent
    mock_deactivate.assert_not_called()


def test_a_stripe_failure_while_closing_links_never_undoes_a_real_payment(db, loft, contact):
    # The money has already been committed by the time the links are
    # closed. A Stripe outage may leave a link live (logged); it may not
    # cost us the payment record.
    booking = _booking_on(db, loft, contact)
    invoice = create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2026, 11, 10), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    record_payment_link(db, invoice, "plink_one")

    with patch.object(stripe_integration, "deactivate_payment_links", side_effect=RuntimeError("stripe down")):
        payment = record_payment(
            db, invoice, amount=Decimal("500.00"), method=PaymentMethod.card,
            reference="pi_stripe_down", actor="test",
        )

    db.refresh(invoice)
    assert payment.id is not None
    assert invoice.status == InvoiceStatus.paid


# --- completing an event is not the same as voiding it (review, 2026-09-04) ---
#
# TERMINAL_STATUSES lumps `completed`/`archived` in with `cancelled`/`dead`,
# so the ordinary post-event step used to cancel the client's unpaid
# balance, kill its card link, and 410 the receipt and signed contract of
# a client who had already paid in full. Completion is a statement about
# the event having happened, not about the money being settled.


def _completed_booking_with_sent_invoice(db, space, contact):
    booking = _booking_on(db, space, contact)
    transition_status(db, booking, BookingStatus.offered, actor="staff:test")
    transition_status(db, booking, BookingStatus.tentative, actor="staff:test")
    transition_status(db, booking, BookingStatus.confirmed, actor="staff:test")
    invoice = create_invoice(
        db, booking, InvoiceType.final, [{"description": "Balance", "quantity": 1, "unit_price": "1200.00"}],
        dt.date(2026, 11, 25), actor="test",
    )
    mark_invoice_sent(db, invoice, actor="test")
    return booking, invoice


def test_completing_an_event_leaves_an_unpaid_balance_payable(db, loft, contact):
    booking, invoice = _completed_booking_with_sent_invoice(db, loft, contact)
    record_payment_link(db, invoice, "plink_balance")

    with patch.object(stripe_integration, "deactivate_payment_links") as mock_deactivate:
        transition_status(db, booking, BookingStatus.completed, actor="staff:test")

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent, "the balance must stay payable after the event happens"
    mock_deactivate.assert_not_called()


def test_a_completed_bookings_invoice_and_signed_agreement_stay_reachable(db, loft, contact):
    booking, invoice = _completed_booking_with_sent_invoice(db, loft, contact)
    document = create_new_version(db, booking, DocumentType.agreement, {"x": 1}, actor="test")
    mark_document_sent(db, document, actor="test")
    sign(db, document, signer_name="Pat Wilson", signer_ip="203.0.113.9")

    transition_status(db, booking, BookingStatus.completed, actor="staff:test")
    db.refresh(invoice)
    db.refresh(document)

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        assert client.get(f"/i/{invoice.access_token}").status_code == 200
        assert client.get(f"/d/{document.access_token}").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_a_dead_booking_still_kills_everything(db, loft, contact):
    # The Sophie Mavridis guarantee, unchanged by the completed/void split.
    booking, invoice = _completed_booking_with_sent_invoice(db, loft, contact)
    document = create_new_version(db, booking, DocumentType.agreement, {"x": 1}, actor="test")
    mark_document_sent(db, document, actor="test")
    record_payment_link(db, invoice, "plink_dead")

    with patch.object(stripe_integration, "deactivate_payment_links") as mock_deactivate:
        transition_status(db, booking, BookingStatus.cancelled, actor="staff:test")

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.cancelled
    mock_deactivate.assert_called_once_with(["plink_dead"])

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        assert client.get(f"/i/{invoice.access_token}").status_code == 410
        assert client.get(f"/d/{document.access_token}").status_code == 410
    finally:
        app.dependency_overrides.clear()
