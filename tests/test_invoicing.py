import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services.invoicing import (
    calculate_card_payment_amount,
    cancel_invoice,
    create_deposit_invoice,
    create_invoice,
    delete_draft,
    get_by_token,
    get_payment_summary,
    gst_component,
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
        assert "GST included" in resp.text
        assert "45.45" in resp.text
        assert "063-519" in resp.text
        assert "10315591" in resp.text
        assert "[REVIEW]" not in resp.text
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
