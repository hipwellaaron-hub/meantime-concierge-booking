"""A sent final invoice tells the client what is payable NOW.

From the stale-copy register, the only entry that can bill a client twice,
and the half the detector could not reach: the client's own page.

The "Less: deposit credited" line is re-derived on every edit and once at
mark_sent, never again. The ordinary sequence -- final invoice goes out,
THEN the deposit is paid -- leaves the client holding a bill for the full
food total while their deposit sits against a different invoice. The
detector (final_invoice_missing_deposit_credit) raises a flag on Triage, in
the digest and on the booking banner, and the client sees none of those.
They see the invoice page, which printed the full balance twice and minted
a Pay-by-card link for it. Paying what they were shown paid the deposit
twice, and the overpayment guard could not catch it because the amount
equalled invoice.total exactly.

THE STORED INVOICE IS NOT REWRITTEN. invoice.total is the record of what
was asked for, and the detector's docstring is right that rewriting it
behind a client is Revise's job. What a real accounting package prints
beside the total is the OTHER figure: balance due now. That is derived at
render in _build_invoice_context -- the one function the web view, the PDF
and the staff preview all use -- so every invoice already issued gets it
and the three copies cannot disagree.

EVERY PROBE FORCES THE STALE CONDITION. A deposit paid before the invoice
went out is already credited on its lines, so a page that ignored the new
figure entirely would still print the right number for that sequence. The
assertions here only hold if the deposit lands AFTER mark_sent.
"""
import datetime as dt
import re
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing, stripe_integration
from app.services.booking import create_booking


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _sent_final(db, booking, total="1450.00"):
    inv = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": total}],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()
    return inv


def _deposit(db, booking, *, paid):
    dep = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=5), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, dep, actor="staff:test@meantime.com.au")
    db.flush()
    if Decimal(paid) > 0:
        invoicing.record_payment(
            db, dep, amount=Decimal(paid), method=PaymentMethod.bank_transfer, actor="test"
        )
        db.flush()
    return dep


@pytest.fixture()
def client(db, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _owing_figures(html):
    """Every dollar figure printed under an 'Amount Owing' label, in order."""
    return re.findall(r"Amount Owing</td><td>\$([\d,]+\.\d\d)", html)


# --- the figure ---------------------------------------------------------------


def test_the_deposit_paid_after_send_is_not_charged_again(db, hamilton, loft, contact):
    """THE one. Out at $1,450 with no credit; $500 deposit lands afterwards."""
    booking = _booking(db, loft, contact, "ZZPAYABLE Late")
    final = _sent_final(db, booking)
    assert invoicing.uncredited_deposit(db, final) == Decimal("0.00"), (
        "nothing is uncredited before the deposit is paid"
    )

    _deposit(db, booking, paid="500.00")

    assert invoicing.uncredited_deposit(db, final) == Decimal("500.00")
    assert final.total == Decimal("1450.00"), "the issued invoice must not be rewritten"


def test_a_deposit_credited_before_send_is_not_credited_twice(db, hamilton, loft, contact):
    """The other direction. mark_sent already put the credit on the lines;
    subtracting it again at render would under-bill by the deposit."""
    booking = _booking(db, loft, contact, "ZZPAYABLE Early")
    _deposit(db, booking, paid="500.00")
    final = _sent_final(db, booking)

    assert final.total == Decimal("950.00"), "mark_sent did not apply the credit"
    assert invoicing.uncredited_deposit(db, final) == Decimal("0.00")


def test_only_the_part_paid_since_send_is_new(db, hamilton, loft, contact):
    """$300 in before send and credited; $200 more after. Exactly $200 is
    uncredited -- not $500, not $0."""
    booking = _booking(db, loft, contact, "ZZPAYABLE Part")
    dep = _deposit(db, booking, paid="300.00")
    final = _sent_final(db, booking)
    assert final.total == Decimal("1150.00")

    invoicing.record_payment(
        db, dep, amount=Decimal("200.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()

    assert invoicing.uncredited_deposit(db, final) == Decimal("200.00")


def test_a_deposit_invoice_never_credits_itself(db, hamilton, loft, contact):
    """PART-paid, deliberately. A fully paid deposit is status 'paid' and
    the paid branch answers zero before the type check is ever reached --
    so with the type check deleted this still passed. Mutation-checked, and
    that is what it caught. At $200 of $500 the invoice stays 'sent', and
    the ONLY thing that can make the answer zero is that it is a deposit."""
    booking = _booking(db, loft, contact, "ZZPAYABLE DepositSelf")
    dep = _deposit(db, booking, paid="200.00")
    assert dep.status == InvoiceStatus.sent, "the fixture must not reach the paid branch"

    assert invoicing.uncredited_deposit(db, dep) == Decimal("0.00")


def test_a_paid_final_has_nothing_payable(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZPAYABLE Paid")
    final = _sent_final(db, booking)
    invoicing.record_payment(
        db, final, amount=Decimal("1450.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    _deposit(db, booking, paid="500.00")

    assert final.status == InvoiceStatus.paid
    assert invoicing.uncredited_deposit(db, final) == Decimal("0.00")


# --- the page ------------------------------------------------------------------


def test_the_client_page_prints_what_is_payable_now_in_both_places(client, db, loft, contact):
    booking = _booking(db, loft, contact, "ZZPAYABLE Page")
    final = _sent_final(db, booking)
    _deposit(db, booking, paid="500.00")

    page = client.get(f"/i/{final.access_token}")
    assert page.status_code == 200

    assert _owing_figures(page.text) == ["950.00", "950.00"], (
        f"the two Amount Owing cells disagree or still bill the deposit: {_owing_figures(page.text)}"
    )
    assert "Total Incl Tax</td><td>$1450.00" in page.text, "the record of what was asked for moved"
    assert "Less: deposit received" in page.text


def test_the_page_names_the_invoice_the_deposit_sits_on(client, db, loft, contact):
    booking = _booking(db, loft, contact, "ZZPAYABLE Named")
    final = _sent_final(db, booking)
    dep = _deposit(db, booking, paid="500.00")

    page = client.get(f"/i/{final.access_token}")

    assert f"Less: deposit received on {dep.invoice_reference}" in page.text


def test_the_page_prints_no_credit_row_when_nothing_is_uncredited(client, db, loft, contact):
    """A row that always prints is a row nobody reads."""
    booking = _booking(db, loft, contact, "ZZPAYABLE NoRow")
    _deposit(db, booking, paid="500.00")
    final = _sent_final(db, booking)

    page = client.get(f"/i/{final.access_token}")

    assert "Less: deposit received" not in page.text
    assert _owing_figures(page.text) == ["950.00", "950.00"]


def test_the_staff_preview_shows_the_same_figure(admin_client, db, loft, contact):
    """Aaron's rule: read a client document through the staff preview. It
    must show what the client sees, from the same context function."""
    booking = _booking(db, loft, contact, "ZZPAYABLE Staff")
    final = _sent_final(db, booking)
    _deposit(db, booking, paid="500.00")

    page = admin_client.get(f"/admin/bookings/{booking.id}/invoices/{final.id}/preview")
    assert page.status_code == 200

    assert _owing_figures(page.text) == ["950.00", "950.00"]


def test_the_pdf_context_carries_the_same_figure(db, hamilton, loft, contact):
    """The PDF renders through _build_invoice_context too. Asserted on the
    context rather than on rendered PDF bytes, which is what the download
    route feeds to the renderer."""
    from app.api.invoices import _build_invoice_context

    booking = _booking(db, loft, contact, "ZZPAYABLE PDF")
    final = _sent_final(db, booking)
    _deposit(db, booking, paid="500.00")

    context = _build_invoice_context(db, final, include_card_payment=False)

    assert context["payable_now"] == Decimal("950.00")
    assert context["uncredited_deposit"] == Decimal("500.00")
    assert context["summary"]["balance_due"] == Decimal("1450.00"), (
        "balance_due is the invoice's own figure and must stay what it was"
    )


# --- the card link -------------------------------------------------------------


def test_the_card_link_is_minted_for_what_is_payable_now(db, hamilton, loft, contact):
    """The overpayment guard cannot catch a card payment for the stale full
    balance, because the amount equals invoice.total exactly. So the link
    must be minted for the right figure in the first place.

    is_configured_for is patched TRUE and create_payment_link stubbed: in
    the test environment Stripe is unconfigured, so without the patch no
    link is minted whatever amount the route chose and the assertion would
    pass with the fix deleted -- the same shape the preview test had.
    """
    from app.api.invoices import _build_invoice_context

    booking = _booking(db, loft, contact, "ZZPAYABLE Card")
    final = _sent_final(db, booking)
    _deposit(db, booking, paid="500.00")

    minted = {}

    def _fake_link(invoice, amount):
        minted["amount"] = amount
        return "https://pay.example/link", "plink_test", "acct_test"

    with patch.object(stripe_integration, "is_configured_for", return_value=True), \
         patch.object(stripe_integration, "create_payment_link", side_effect=_fake_link), \
         patch.object(invoicing, "record_payment_link"):
        context = _build_invoice_context(db, final, include_card_payment=True)

    assert minted["amount"] == Decimal("950.00"), (
        f"the card link was minted for {minted.get('amount')}, which bills the deposit twice"
    )
    assert context["card_payment_amount"] == Decimal("950.00")
