"""The Event Order's "Total paid" counts every payment, not just the deposit.

beo_billing computed the payment side live, which fixed the frozen figure
-- and left the LABEL wrong. "Total paid" printed deposit_paid, which is
get_deposit_paid: payments against DEPOSIT invoices only. A part payment
against the final invoice is money the client has handed over and it was
invisible here, so "Total paid" understated it and "Balance owing"
overstated it, on the client's screen and PDF and the admin's.

The "Less deposit paid" bullet stays deposit-only: that line is about the
deposit. "Total paid" and "Balance owing" are about everything.

EVERY PROBE PAYS SOMETHING AGAINST THE FINAL. With only a deposit paid the
two figures coincide, and a template still printing deposit_paid under
the "Total paid" label would pass.
"""
import datetime as dt
from decimal import Decimal

from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import beo_proposals, documents as documents_service, invoicing
from app.services.booking import create_booking
from app.templating import beo_billing

FOOD = [
    {"description": "Grazing Platter", "quantity": 4, "unit_price": "250.00", "category": "platters"},
    {"description": "Pizza", "quantity": 9, "unit_price": "50.00", "category": "pizza"},
]  # 1450.00


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=12), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=80, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _beo(db, booking):
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {"line_items": FOOD, "note": None}
    from app.services.document_generation import build_total_food_spend, compute_food_order_total

    content["total_food_spend"] = build_total_food_spend(compute_food_order_total(FOOD), Decimal("0.00"))
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    return doc


def _paid(db, booking, kind, total, paid):
    inv = invoicing.create_invoice(
        db, booking, kind, [{"description": "Line", "quantity": 1, "unit_price": total}],
        dt.date.today() + dt.timedelta(days=7), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    if Decimal(paid) > 0:
        invoicing.record_payment(db, inv, amount=Decimal(paid), method=PaymentMethod.bank_transfer, actor="test")
    db.flush()
    return inv


def test_a_part_payment_on_the_final_counts_towards_total_paid(db, hamilton, loft, contact):
    """THE one. $500 deposit and $300 against the final: $800 paid, $650 owing."""
    booking = _booking(db, loft, contact, "ZZPAID Part")
    doc = _beo(db, booking)
    _paid(db, booking, InvoiceType.deposit, "500.00", "500.00")
    _paid(db, booking, InvoiceType.final, "950.00", "300.00")

    billing = beo_billing(doc)

    assert billing["total_paid"] == "800.00", f"Total paid ignores the final's payment: {billing}"
    assert billing["balance_due"] == "650.00", f"Balance owing overstated: {billing}"


def test_the_deposit_bullet_stays_deposit_only(db, hamilton, loft, contact):
    """'Less deposit paid' is about the deposit. It must not absorb the
    final's part payment and print $800 under a deposit label."""
    booking = _booking(db, loft, contact, "ZZPAID DepositLine")
    doc = _beo(db, booking)
    _paid(db, booking, InvoiceType.deposit, "500.00", "500.00")
    _paid(db, booking, InvoiceType.final, "950.00", "300.00")

    assert beo_billing(doc)["deposit_paid"] == "500.00"


def test_with_only_a_deposit_the_figures_agree(db, hamilton, loft, contact):
    """The ordinary case, and the one that could never tell the two apart."""
    booking = _booking(db, loft, contact, "ZZPAID DepositOnly")
    doc = _beo(db, booking)
    _paid(db, booking, InvoiceType.deposit, "500.00", "500.00")

    billing = beo_billing(doc)

    assert billing["total_paid"] == billing["deposit_paid"] == "500.00"
    assert billing["balance_due"] == "950.00"


def test_nothing_paid_is_a_fact_not_an_unknown(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZPAID Nothing")
    doc = _beo(db, booking)

    billing = beo_billing(doc)

    assert billing["total_paid"] == "0.00"
    assert billing["balance_due"] == "1450.00"


def test_the_rendered_event_order_prints_every_payment(db, hamilton, loft, contact):
    """Both 'Total paid' sites -- the header and the Billing Summary -- and
    'Balance owing', on the client's page."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    booking = _booking(db, loft, contact, "ZZPAID Rendered")
    doc = _beo(db, booking)
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    _paid(db, booking, InvoiceType.deposit, "500.00", "500.00")
    _paid(db, booking, InvoiceType.final, "950.00", "300.00")

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/d/{doc.access_token}").text
    finally:
        app.dependency_overrides.clear()

    assert "A$800.00" in page, "the header's Total Paid still prints the deposit only"
    assert "$800.00" in page and "$650.00" in page
    assert "$1150.00" not in page, "a Balance owing that ignores the final's payment is on the page"


def test_every_total_paid_site_in_the_template_reads_total_paid():
    """STRUCTURAL, and here is why. document.html renders the header twice
    -- once in the PDF branch (`{% elif is_pdf %}`), once for the page --
    and the client page test above reaches only the second. A mutation
    that put the deposit-only figure back on the PDF header survived it,
    because nothing renders a PDF and greps its bytes. Two paths that the
    behavioural test cannot tell apart need a check on the source, and
    this is it: every "Total paid" site reads billing.total_paid, none
    reads billing.deposit_paid, and the one line that is ABOUT the deposit
    still does."""
    import pathlib

    lines = pathlib.Path("app/templates/document.html").read_text(encoding="utf-8").splitlines()
    total_paid_sites = [
        ln for ln in lines if "Total Paid:</span>" in ln or "<td>Total paid</td>" in ln
    ]
    assert len(total_paid_sites) == 4, f"expected the two headers and two summary rows, found {len(total_paid_sites)}"
    for ln in total_paid_sites:
        assert "billing.total_paid" in ln, f"a Total paid site does not read total_paid: {ln.strip()[:120]}"
        assert "billing.deposit_paid" not in ln, f"a Total paid site still prints the deposit only: {ln.strip()[:120]}"

    deposit_lines = [ln for ln in lines if "Less deposit paid:" in ln]
    assert len(deposit_lines) == 1
    assert "billing.deposit_paid" in deposit_lines[0], "the deposit bullet must stay about the deposit"
