"""An Event Order shows the deposit that has been paid, on every copy.

Aaron, 2026-09-14, on a live booking twelve days out: "If someone has paid
a deposit, that deposit needs to be taken off the event order. We need a
better way than doing this manually. I can't go through the rest of the 32
bookings."

WHAT WAS WRONG, and it was two things wearing one coat:

  * `fresh_beo_content` -- what the Generate button calls -- invoked
    `generate_beo_content(booking)` without `deposit_paid`, which defaults
    to None, and None is what makes build_total_food_spend print
    "[REVIEW] deposit paid / balance due aren't derivable yet -- payments
    aren't tracked in Concierge until Phase 3". A sentence that stopped
    being true when payments started being tracked, printed over a figure
    the invoices table was holding. The wizard path passed it; this one
    never did.

  * Even with that fixed, the figure was FROZEN into the document at
    generation. A balance frozen at generation is wrong the moment anybody
    pays anything, and it would have left every Event Order already issued
    still wrong -- 32 of them, each needing a regenerate.

So the payment side is computed AT RENDER, which is this codebase's own
stated rule ("Live (not frozen) is still the rule for the INVOICE...
Signed agreements deliberately do the opposite") and its established
mechanism ("it works on every document that already exists, without
regenerating any of them").

The food total stays frozen: what was ordered is a fact about this version
of the document. Only what has been PAID is live.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Invoice, Payment
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import beo_proposals, documents as documents_service, invoicing
from app.services.booking import change_status, create_booking

FOOD = [
    {"description": "Grazing Platter", "quantity": 4, "unit_price": "250.00", "category": "platters"},
    {"description": "Pizza", "quantity": 9, "unit_price": "50.00", "category": "pizza"},
]
FOOD_TOTAL = Decimal("1450.00")


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _booking(db, loft, contact, name="Deposit On The Order"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=12), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=80, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _legacy_deposit(db, booking, amount="500.00"):
    """A migrated iVvy deposit: a paid invoice AND a real Payment row,
    exactly as app/services/concierge_migration.py builds one."""
    invoice = Invoice(
        booking_id=booking.id, type=InvoiceType.deposit,
        line_items=[{"description": "Booking deposit (paid in iVvy, migrated)",
                     "quantity": 1, "unit_price": amount}],
        subtotal=Decimal(amount), surcharge=Decimal("0.00"), total=Decimal(amount),
        status=InvoiceStatus.paid, due_date=dt.date(2026, 1, 12),
        paid_at=dt.datetime(2026, 1, 12, 12, tzinfo=dt.timezone.utc),
        is_legacy=True, legacy_source_ref="7HB7XPF8RP",
    )
    db.add(invoice)
    db.flush()
    db.add(Payment(
        invoice_id=invoice.id, amount=Decimal(amount), method=PaymentMethod.bank_transfer,
        reference="MIGRATED from iVvy", received_at=dt.datetime(2026, 1, 12, 12, tzinfo=dt.timezone.utc),
    ))
    db.flush()
    return invoice


def _beo(db, booking, *, food=FOOD):
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {"line_items": food, "note": None}
    from app.services.document_generation import build_total_food_spend, compute_food_order_total

    content["total_food_spend"] = build_total_food_spend(compute_food_order_total(food), None)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="test"
    )
    db.flush()
    return document


# --- the headline --------------------------------------------------------


def test_a_paid_deposit_comes_off_the_event_order(db, hamilton, loft, contact):
    """THE one. Food 1,450, deposit 500, balance 950 -- Aaron's figures."""
    booking = _booking(db, loft, contact)
    _legacy_deposit(db, booking)
    document = _beo(db, booking)

    from app.templating import beo_billing

    billing = beo_billing(document)

    assert billing["total"] == "1450.00"
    assert billing["deposit_paid"] == "500.00"
    assert billing["balance_due"] == "950.00"


def test_a_document_stored_before_this_change_is_fixed_without_regenerating(
    db, hamilton, loft, contact
):
    """The 32 bookings. Their stored content holds the [REVIEW] note and no
    deposit -- and must still print the right figures, because a regenerate
    for each is the manual work this exists to avoid."""
    booking = _booking(db, loft, contact, name="Already Issued")
    _legacy_deposit(db, booking)
    document = _beo(db, booking)

    stored = document.content["total_food_spend"]
    assert stored["deposit_paid"] is None, "the stored block is the old shape"
    assert "Phase 3" in (stored["note"] or ""), "the stored note is the old one"

    from app.templating import beo_billing

    assert beo_billing(document)["balance_due"] == "950.00"


def test_no_deposit_paid_reads_zero_rather_than_a_dash(db, hamilton, loft, contact):
    """0.00 is a FACT -- nobody has paid yet -- and a different statement
    from "we cannot tell". A dash on a money line reads as the second."""
    booking = _booking(db, loft, contact, name="Nothing Paid")
    document = _beo(db, booking)

    from app.templating import beo_billing

    billing = beo_billing(document)

    assert billing["deposit_paid"] == "0.00"
    assert billing["balance_due"] == "1450.00"


def test_a_deposit_paid_after_the_order_was_issued_still_shows(db, hamilton, loft, contact):
    """The reason this is live rather than a better freeze. A balance
    frozen at generation is wrong the moment anybody pays anything."""
    booking = _booking(db, loft, contact, name="Paid Afterwards")
    document = _beo(db, booking)

    from app.templating import beo_billing

    assert beo_billing(document)["balance_due"] == "1450.00"

    _legacy_deposit(db, booking, amount="500.00")

    assert beo_billing(document)["balance_due"] == "950.00", (
        "a payment taken after the Event Order was issued never reached it"
    )


# --- what each copy actually prints ---------------------------------------


@pytest.mark.parametrize("staff_preview", [False, True])
def test_every_rendered_copy_shows_the_balance(db, hamilton, loft, contact, staff_preview):
    """Six routes render this template. The figure comes from a filter
    rather than six route contexts precisely so none of them can forget
    it -- so both the client's copy and the staff preview are checked."""
    booking = _booking(db, loft, contact, name="Rendered Copy")
    _legacy_deposit(db, booking)
    document = _beo(db, booking)
    documents_service.mark_sent(db, document, actor="test")
    db.flush()

    try:
        resp = _client(db).get(f"/d/{document.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert "$500.00" in resp.text, "the deposit is not on the page"
    assert "$950.00" in resp.text, "the balance owing is not on the page"
    assert "Phase 3" not in resp.text, "the stale payments note is still printing"


def test_the_phase_three_note_is_gone_from_the_template():
    """It said payments are not tracked in Concierge. They are, and it was
    printing over a figure the system was holding."""
    import pathlib

    markup = pathlib.Path("app/templates/document.html").read_text(encoding="utf-8")

    assert "total_food_spend.note" not in markup
    assert "total_food_spend.deposit_paid" not in markup, (
        "a money block still reads the frozen deposit"
    )
    assert "total_food_spend.balance_due" not in markup, (
        "a money block still reads the frozen balance"
    )
