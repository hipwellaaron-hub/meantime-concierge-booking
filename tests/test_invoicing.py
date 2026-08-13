import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services.invoicing import (
    calculate_card_payment_amount,
    cancel_invoice,
    create_deposit_invoice,
    create_final_invoice,
    create_invoice,
    delete_draft,
    get_by_token,
    get_deposit_paid,
    get_payment_summary,
    gst_component,
    has_active_final_invoice,
    is_public_holiday,
    mark_sent,
    record_payment,
)
from app.services.invoicing import compute_totals
from app.services.policy import CARD_SURCHARGE_BAN_DATE

CATERING_ITEMS = [
    {"description": "Antipasto platter", "quantity": 4, "unit_price": "65.00"},
    {"description": "Margherita pizza", "quantity": 10, "unit_price": "18.00"},
]  # subtotal = 440.00


def test_deposit_invoice_matches_policy(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    assert invoice.type == InvoiceType.deposit
    assert invoice.subtotal == Decimal("500.00")
    assert invoice.surcharge == Decimal("0.00")
    assert invoice.total == Decimal("500.00")


# --- invoice_number -----------------------------------------------------------


def test_invoice_gets_a_real_sequential_number(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    assert isinstance(invoice.invoice_number, int)
    assert invoice.invoice_number > 0


def test_two_invoices_get_different_numbers(db, booking):
    first = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    second = create_invoice(
        db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 9, 15), actor="test",
    )
    assert first.invoice_number != second.invoice_number


# --- per-line GST breakdown ----------------------------------------------------


def test_line_item_breakdown_splits_gst_per_line():
    from app.services.invoicing import line_item_breakdown

    rows = line_item_breakdown([{"description": "Catering", "quantity": 1, "unit_price": "110.00"}])
    assert len(rows) == 1
    assert rows[0]["amount_incl"] == Decimal("110.00")
    assert rows[0]["tax_amount"] == Decimal("10.00")
    assert rows[0]["amount_excl"] == Decimal("100.00")


def test_line_item_breakdown_handles_negative_credit_lines():
    from app.services.invoicing import line_item_breakdown

    rows = line_item_breakdown([{"description": "Less: deposit credited", "quantity": 1, "unit_price": "-500.00"}])
    assert rows[0]["amount_incl"] == Decimal("-500.00")
    # 1/11 of a negative amount is still negative -- the credit's own GST
    # component nets correctly against the gross line it offsets.
    assert rows[0]["tax_amount"] == Decimal("-45.45")


def test_no_surcharge_on_non_holiday_date(db, booking, public_holidays):
    # booking.event_date is 2026-10-03, a Saturday, not a public holiday
    invoice = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 10, 1), actor="test")
    assert invoice.subtotal == Decimal("440.00")
    assert invoice.surcharge == Decimal("0.00")
    assert invoice.total == Decimal("440.00")


def test_surcharge_applied_on_public_holiday(db, hamilton, loft, public_holidays):
    from app.services.booking import create_booking

    christmas_booking = create_booking(
        db,
        space_id=loft.id,
        contact_id=None,
        event_date=dt.date(2026, 12, 25),
        start_time=dt.time(12, 0),
        end_time=dt.time(17, 0),
        event_name="Christmas Party",
        event_type="corporate",
        adult_count=50,
        child_count=0,
        notes=None,
        actor="test",
    )
    invoice = create_invoice(db, christmas_booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 12, 20), actor="test")
    assert invoice.surcharge == Decimal("44.00")  # 10% of 440.00
    assert invoice.total == Decimal("484.00")


def test_bank_holiday_does_not_trigger_surcharge(db, public_holidays):
    # Bank Holiday is finance-sector only -- see app/models/public_holiday.py
    assert is_public_holiday(db, dt.date(2026, 8, 3)) is False


def test_christmas_is_flagged_as_public_holiday(db, public_holidays):
    assert is_public_holiday(db, dt.date(2026, 12, 25)) is True


def test_card_surcharge_applied_before_ban_date():
    amount = calculate_card_payment_amount(Decimal("100.00"), dt.date(2026, 9, 1))
    assert amount == Decimal("101.80")  # 1.8% surcharge


def test_card_surcharge_blocked_after_rba_ban_date():
    amount = calculate_card_payment_amount(Decimal("100.00"), CARD_SURCHARGE_BAN_DATE)
    assert amount == Decimal("100.00")


def test_amex_still_surchargeable_after_ban_date():
    amount = calculate_card_payment_amount(Decimal("100.00"), CARD_SURCHARGE_BAN_DATE, card_network="amex")
    assert amount == Decimal("101.80")


def test_single_full_payment_marks_invoice_paid(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")

    record_payment(db, invoice, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")

    assert invoice.status.value == "paid"
    assert invoice.paid_at is not None


def test_split_payment_across_three_payers(db, booking):
    invoice = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 10, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    third = (invoice.total / 3).quantize(Decimal("0.01"))

    record_payment(db, invoice, amount=third, method=PaymentMethod.bank_transfer, payer_name="Org A", actor="test")
    summary = get_payment_summary(db, invoice)
    assert summary["is_fully_paid"] is False
    assert len(summary["payments"]) == 1

    record_payment(db, invoice, amount=third, method=PaymentMethod.bank_transfer, payer_name="Org B", actor="test")
    summary = get_payment_summary(db, invoice)
    assert summary["is_fully_paid"] is False
    assert {p.payer_name for p in summary["payments"]} == {"Org A", "Org B"}

    remaining = invoice.total - (third * 2)
    record_payment(db, invoice, amount=remaining, method=PaymentMethod.bank_transfer, payer_name="Org C", actor="test")
    summary = get_payment_summary(db, invoice)
    assert summary["is_fully_paid"] is True
    assert summary["balance_due"] == Decimal("0.00")
    assert invoice.status.value == "paid"


def test_cannot_pay_a_cancelled_invoice(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    cancel_invoice(db, invoice, actor="test")

    with pytest.raises(ValueError):
        record_payment(db, invoice, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")


def test_cannot_send_invoice_twice(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    with pytest.raises(ValueError):
        mark_sent(db, invoice, actor="test")


# --- email-validity guard on the send path --------------------------------


def test_cannot_send_invoice_when_booking_has_no_contact(db, loft):
    from app.services.booking import create_booking as _create_booking

    contactless_booking = _create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="No Contact Booking",
        event_type="party", adult_count=10, child_count=0, notes=None, actor="test",
    )
    invoice = create_deposit_invoice(db, contactless_booking, due_date=dt.date(2027, 5, 1), actor="test")
    with pytest.raises(ValueError, match="valid email"):
        mark_sent(db, invoice, actor="test")
    assert invoice.status == InvoiceStatus.draft


def test_cannot_send_invoice_when_contact_email_is_malformed(db, loft):
    from app.models import Contact
    from app.services.booking import create_booking as _create_booking

    bad_contact = Contact(name="Bad Email Contact", email="not-an-email")
    db.add(bad_contact)
    db.flush()
    bad_booking = _create_booking(
        db, space_id=loft.id, contact_id=bad_contact.id, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="Bad Email Booking",
        event_type="party", adult_count=10, child_count=0, notes=None, actor="test",
    )
    invoice = create_deposit_invoice(db, bad_booking, due_date=dt.date(2027, 5, 1), actor="test")
    with pytest.raises(ValueError, match="valid email"):
        mark_sent(db, invoice, actor="test")


def test_draft_invoice_not_publicly_viewable(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_card_surcharge_permitted_day_before_ban():
    amount = calculate_card_payment_amount(Decimal("100.00"), dt.date(2026, 9, 30))
    assert amount == Decimal("101.80")


def test_card_surcharge_blocked_day_after_ban():
    amount = calculate_card_payment_amount(Decimal("100.00"), dt.date(2026, 10, 2))
    assert amount == Decimal("100.00")


def test_card_surcharge_blocked_exact_midnight_of_ban_date():
    # CARD_SURCHARGE_BAN_DATE itself must already be banned (inclusive) --
    # "from 1 October 2026" means the ban applies starting that day.
    assert calculate_card_payment_amount(Decimal("100.00"), CARD_SURCHARGE_BAN_DATE) == Decimal("100.00")


def test_cannot_cancel_an_already_cancelled_invoice(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    cancel_invoice(db, invoice, actor="test")
    with pytest.raises(ValueError):
        cancel_invoice(db, invoice, actor="test")


def test_cannot_pay_a_draft_invoice(db, booking):
    """Out-of-order event: a draft invoice was never sent, so a client
    could never have obtained its token -- a payment attempt here can
    only be a bug in the caller, not a real client action."""
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    assert invoice.status.value == "draft"
    with pytest.raises(ValueError):
        record_payment(db, invoice, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")


def test_negative_payment_amount_rejected(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    with pytest.raises(ValueError):
        record_payment(db, invoice, amount=Decimal("-50.00"), method=PaymentMethod.bank_transfer, actor="test")


def test_zero_payment_amount_rejected(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    with pytest.raises(ValueError):
        record_payment(db, invoice, amount=Decimal("0.00"), method=PaymentMethod.bank_transfer, actor="test")


def test_overpayment_does_not_crash_and_marks_paid(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    record_payment(db, invoice, amount=Decimal("600.00"), method=PaymentMethod.bank_transfer, actor="test")

    assert invoice.status.value == "paid"
    summary = get_payment_summary(db, invoice)
    assert summary["balance_due"] == Decimal("-100.00")  # informational credit, not an error


def test_compute_totals_malformed_line_item_raises_clean_error(db, booking):
    bad_items = [{"description": "Broken", "quantity": "lots", "unit_price": "18.00"}]
    with pytest.raises(ValueError):
        compute_totals(db, booking.event_date, bad_items)


def test_compute_totals_missing_key_raises_clean_error(db, booking):
    bad_items = [{"description": "Broken", "quantity": 1}]  # no unit_price
    with pytest.raises(ValueError):
        compute_totals(db, booking.event_date, bad_items)


def test_invoice_null_byte_token_returns_404_not_500(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/i/token%00.txt")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_invoice_view_unknown_token_returns_404_not_500(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/i/not-a-real-token")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_paid_at_and_received_at_displayed_in_sydney_time_not_utc(db, booking):
    """Same regression as documents: paid_at/received_at are stored
    UTC-aware, and the template used to render them with a raw
    .strftime(), which prints UTC rather than the Sydney date the
    payment was actually received on."""
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    # 10am Sydney (UTC+11, daylight saving) on 7 March is 11pm UTC on 6 March.
    received_at = dt.datetime(2027, 3, 6, 23, 0, tzinfo=dt.timezone.utc)
    record_payment(
        db, invoice, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, received_at=received_at, actor="test"
    )

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        assert "07 Mar 2027" in resp.text
        assert "06 Mar 2027" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_invoice_view_shows_totals_and_split_payments(db, booking):
    invoice = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 10, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    record_payment(db, invoice, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, payer_name="Org A", actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        assert "Antipasto platter" in resp.text
        assert "Org A" in resp.text
        assert "240.00" in resp.text  # balance due (440 - 200)
        # Stripe isn't configured in this build -- must not silently claim it is
        assert "Card payment is available on request" in resp.text
    finally:
        app.dependency_overrides.clear()


# --- link-open tracking (viewed_at) ---------------------------------------------


def test_invoice_view_records_viewed_at_once(db, booking):
    invoice = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 10, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    assert invoice.viewed_at is None

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.get(f"/i/{invoice.access_token}")
        db.refresh(invoice)
        assert invoice.viewed_at is not None
        assert invoice.status == InvoiceStatus.sent  # unchanged -- see the field's own comment

        first_viewed_at = invoice.viewed_at
        client.get(f"/i/{invoice.access_token}")  # a second visit must not move the timestamp
        db.refresh(invoice)
        assert invoice.viewed_at == first_viewed_at
    finally:
        app.dependency_overrides.clear()


def test_invoice_still_counts_as_unpaid_after_being_viewed(db, hamilton, booking):
    """The whole reason viewed_at is a separate field rather than a new
    status: every "what's still owed" query reads status == sent, and a
    viewed invoice must not silently vanish from those."""
    from app.services.digest import get_overdue_invoices

    invoice = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2020, 1, 1), actor="test")
    mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.get(f"/i/{invoice.access_token}")
    finally:
        app.dependency_overrides.clear()

    db.refresh(invoice)
    assert invoice.viewed_at is not None
    overdue = get_overdue_invoices(db, hamilton, as_of=dt.date(2026, 1, 1))
    assert invoice.id in {o.invoice.id for o in overdue}


# --- GST breakdown --------------------------------------------------------------


def test_gst_component_is_one_eleventh_of_gst_inclusive_total():
    assert gst_component(Decimal("500.00")) == Decimal("45.45")


def test_gst_component_rounds_to_cents():
    assert gst_component(Decimal("100.00")) == Decimal("9.09")


def test_invoice_pdf_shows_gst_and_bank_details(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice = mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        assert "GST Included" in resp.text
        assert "45.45" in resp.text
        assert "063-519" in resp.text
        assert "10315591" in resp.text
        assert "[REVIEW]" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_invoice_page_shows_invoice_number_and_booking_details(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice = mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        assert str(invoice.invoice_number) in resp.text
        assert booking.reference_code in resp.text
        assert booking.event_name in resp.text
    finally:
        app.dependency_overrides.clear()


def test_invoice_page_lists_the_bookings_other_invoices(db, booking):
    deposit = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    deposit = mark_sent(db, deposit, actor="test")
    final = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 9, 15), actor="test")
    final = mark_sent(db, final, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{deposit.access_token}")
        assert resp.status_code == 200
        assert str(final.invoice_number) in resp.text  # the OTHER invoice's number, cross-referenced

        resp2 = client.get(f"/i/{final.access_token}")
        assert str(deposit.invoice_number) in resp2.text
    finally:
        app.dependency_overrides.clear()


def test_invoice_page_draft_invoices_never_appear_in_related_list(db, booking):
    deposit = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    deposit = mark_sent(db, deposit, actor="test")
    draft_final = create_invoice(db, booking, InvoiceType.final, CATERING_ITEMS, dt.date(2026, 9, 15), actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{deposit.access_token}")
        assert resp.status_code == 200
        assert str(draft_final.invoice_number) not in resp.text
    finally:
        app.dependency_overrides.clear()


# --- PDF download ----------------------------------------------------------------


def test_draft_invoice_pdf_download_404s(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}/pdf")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_sent_invoice_pdf_downloads_as_a_real_pdf(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice = mark_sent(db, invoice, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/i/{invoice.access_token}/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"
    finally:
        app.dependency_overrides.clear()


# --- draft-only delete -------------------------------------------------------------


def test_delete_draft_invoice_succeeds(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice_id = invoice.id
    delete_draft(db, invoice, actor="test")
    assert db.get(type(invoice), invoice_id) is None


def test_delete_sent_invoice_is_rejected(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice = mark_sent(db, invoice, actor="test")
    with pytest.raises(ValueError):
        delete_draft(db, invoice, actor="test")
    assert get_by_token(db, invoice.access_token) is not None


def test_delete_paid_invoice_is_rejected(db, booking):
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    invoice = mark_sent(db, invoice, actor="test")
    record_payment(db, invoice, amount=Decimal("500.00"), method=PaymentMethod.card, actor="test")
    with pytest.raises(ValueError):
        delete_draft(db, invoice, actor="test")


# --- Manual final invoice (no wizard required) ----------------------------------


def test_create_final_invoice_with_no_deposit(db, booking):
    invoice = create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")
    assert invoice.type == InvoiceType.final
    assert invoice.total == Decimal("440.00")
    assert not any(li["description"].startswith("Less:") for li in invoice.line_items)


def test_create_final_invoice_credits_a_paid_deposit(db, booking):
    deposit = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")
    mark_sent(db, deposit, actor="test")
    record_payment(db, deposit, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test")

    assert get_deposit_paid(db, booking) == Decimal("500.00")

    invoice = create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")
    assert invoice.subtotal == Decimal("440.00")  # surcharge/subtotal unaffected by the credit
    assert invoice.total == Decimal("-60.00")  # 440 - 500 deposit credit
    credit_lines = [li for li in invoice.line_items if li["description"] == "Less: deposit credited"]
    assert len(credit_lines) == 1
    assert credit_lines[0]["unit_price"] == "-500.00"


def test_create_final_invoice_rejects_a_duplicate(db, booking):
    create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")
    assert has_active_final_invoice(db, booking) is True
    with pytest.raises(ValueError):
        create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")


def test_create_final_invoice_allowed_again_after_cancelling_the_first(db, booking):
    first = create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")
    mark_sent(db, first, actor="test")
    cancel_invoice(db, first, actor="test")
    assert has_active_final_invoice(db, booking) is False

    second = create_final_invoice(db, booking, line_items=CATERING_ITEMS, due_date=dt.date(2026, 10, 1), actor="test")
    assert second.id != first.id
