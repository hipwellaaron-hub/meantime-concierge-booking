"""A paid invoice keeps the bank account the money actually went to.

From the stale-copy register, and the one entry that is the OPPOSITE
failure to the rest: a value read live where a frozen one belongs.

venue_identity reads a venue's bank details live and invoice.html prints
them on every view and every PDF download. That is right for an unpaid
invoice -- the codebase states it: "an unpaid invoice should always point
at the current bank details", because a client about to pay must be told
where the money goes today.

It is wrong for a PAID one. A paid invoice is a receipt: a record of an
account money actually went to. Change a venue's bank details and every
historical paid invoice silently reprints with the new ones.

Harmless while one company had one account. Nice Try Events Pty Ltd is a
separate legal entity with its own bank account, and the day that row is
filled in is the day this stops being theoretical -- which is why it is
fixed before the account goes in rather than after.

NOT BACKFILLED. An invoice paid before this shipped has no record of what
it was paid to, and inventing one from today's venue row would assert a
fact nobody knows. Those keep rendering live and the page says so.
"""
import datetime as dt
from decimal import Decimal

from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Receipt"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _paid_deposit(db, booking):
    inv = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()
    invoicing.record_payment(
        db, inv, amount=inv.total, method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    return inv


# --- the freeze -------------------------------------------------------------


def test_paying_an_invoice_records_the_account_it_was_paid_to(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZRECEIPT Frozen")
    invoice = _paid_deposit(db, booking)

    assert invoice.status == InvoiceStatus.paid
    assert invoice.paid_to_account, "nothing recorded where the money went"
    assert invoice.paid_to_account["bsb"] == hamilton.bank_bsb
    assert invoice.paid_to_account["account_number"] == hamilton.bank_account_number
    assert invoice.paid_to_account["legal_name"] == hamilton.legal_name


def test_an_unpaid_invoice_records_nothing(db, hamilton, loft, contact):
    """It has to point at the CURRENT account -- a client about to pay must
    be told where the money goes today."""
    booking = _booking(db, loft, contact, name="ZZRECEIPT Unpaid")
    inv = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()

    assert inv.paid_to_account is None


def test_a_later_bank_change_does_not_reach_a_paid_invoice(db, hamilton, loft, contact):
    """THE one. This is exactly what happens the day Nice Try Events'
    account is entered."""
    booking = _booking(db, loft, contact, name="ZZRECEIPT BankMoved")
    invoice = _paid_deposit(db, booking)
    paid_to = dict(invoice.paid_to_account)

    hamilton.bank_bsb = "999999"
    hamilton.bank_account_number = "00000000"
    hamilton.bank_account_name = "Some Other Company Pty Ltd"
    db.flush()
    db.refresh(invoice)

    assert invoice.paid_to_account == paid_to, "the receipt followed the venue's new account"


# --- what the client's page actually prints ---------------------------------


def test_the_paid_invoice_page_prints_the_frozen_account(db, hamilton, loft, contact):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    booking = _booking(db, loft, contact, name="ZZRECEIPT Rendered")
    invoice = _paid_deposit(db, booking)
    original_bsb = invoice.paid_to_account["bsb"]

    hamilton.bank_bsb = "999999"
    db.flush()

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/i/{invoice.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert original_bsb in page.text, "the receipt does not show the account it was paid to"
    assert "999999" not in page.text, "the receipt shows an account the client never paid"


def test_an_unpaid_invoice_page_prints_the_current_account(db, hamilton, loft, contact):
    """The other half of the rule, and it must keep working: change the
    account and an unpaid invoice has to follow."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    booking = _booking(db, loft, contact, name="ZZRECEIPT Live")
    inv = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    hamilton.bank_bsb = "123456"
    db.flush()

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/i/{inv.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "123456" in page.text, "an unpaid invoice must point at the current account"


def test_a_pre_existing_paid_invoice_says_it_is_showing_todays_details(
    db, hamilton, loft, contact
):
    """Not backfilled: there is no record of what it was paid to. Saying so
    is the difference between an unknown and a claim."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    booking = _booking(db, loft, contact, name="ZZRECEIPT Legacyshape")
    invoice = _paid_deposit(db, booking)
    invoice.paid_to_account = None  # the shape every invoice paid before today has
    db.flush()

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/i/{invoice.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert "before the account paid to was recorded" in page.text


# --- the whole payee, not just the bank block ---------------------------------


def _client_page(db, invoice):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/i/{invoice.access_token}")
    finally:
        app.dependency_overrides.clear()
    assert page.status_code == 200
    return page.text


def test_the_receipt_freezes_the_company_named_in_the_header(db, hamilton, loft, contact):
    """The first version froze the bank block and left the header live: a
    receipt that recorded which ACCOUNT was paid while reprinting whichever
    COMPANY the venue row named today. Same fault, one section up."""
    booking = _booking(db, loft, contact, name="ZZRECEIPT Header")
    invoice = _paid_deposit(db, booking)
    frozen_abn = invoice.paid_to_account["abn"]
    frozen_name = invoice.paid_to_account["trading_name"]
    assert frozen_abn and frozen_name, "the snapshot must carry the header's fields"

    hamilton.abn = "11 111 111 111"
    hamilton.trading_name = "Renamed Venue"
    hamilton.address = "1 Elsewhere St, Nowhere NSW 0000"
    db.flush()

    html = _client_page(db, invoice)

    assert frozen_abn in html and frozen_name in html
    assert "11 111 111 111" not in html, "the receipt shows an ABN the client never paid"
    assert "Renamed Venue" not in html
    assert "Elsewhere" not in html


def test_the_receipt_says_it_is_showing_the_recorded_account(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZRECEIPT Says")
    invoice = _paid_deposit(db, booking)

    html = _client_page(db, invoice)

    assert "as recorded at the time of payment" in html
    assert "paid before the account paid to was recorded" not in html


def test_a_snapshot_of_blanks_falls_back_to_live_and_says_so(db, hamilton, loft, contact):
    """A venue whose bank fields were empty when the invoice was paid
    froze a dict of blanks -- truthy, so the old template suppressed BOTH
    the live fallback and the disclosure line and printed nothing at all.
    A record holding nothing usable is not a record."""
    booking = _booking(db, loft, contact, name="ZZRECEIPT Blanks")
    invoice = _paid_deposit(db, booking)
    invoice.paid_to_account = {k: "" for k in invoice.paid_to_account}
    db.flush()

    html = _client_page(db, invoice)

    assert hamilton.bank_bsb in html, "a blank snapshot left the client with no account at all"
    assert "paid before the account paid to was recorded" in html


def test_the_staff_preview_context_carries_the_receipt_identity(db, hamilton, loft, contact):
    """The PDF and the staff preview come through the same function as the
    client page; a fix on one path only is the mislabelled-page shape."""
    from app.api.invoices import _build_invoice_context

    booking = _booking(db, loft, contact, name="ZZRECEIPT Context")
    invoice = _paid_deposit(db, booking)
    hamilton.bank_bsb = "999999"
    db.flush()

    context = _build_invoice_context(db, invoice, include_card_payment=False)

    assert context["receipt_identity"]["bsb"] == invoice.paid_to_account["bsb"]
    assert context["receipt_identity"]["bsb"] != "999999"


def test_an_unpaid_invoice_has_no_receipt_identity(db, hamilton, loft, contact):
    from app.api.invoices import _build_invoice_context

    booking = _booking(db, loft, contact, name="ZZRECEIPT NoReceipt")
    inv = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()
    invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
    db.flush()
    # FORCED. An unpaid invoice has no snapshot, so without this the status
    # gate never decides anything and the assertion held with it deleted --
    # mutation-checked, and this is what it caught. Nothing today writes a
    # snapshot onto a sent invoice; a future un-pay or refund path could,
    # and a receipt identity on an invoice that is not a receipt would print
    # a frozen account to a client who has to pay TODAY's.
    inv.paid_to_account = {"account_name": "Frozen Co", "bsb": "111111", "account_number": "22222222"}
    db.flush()

    assert _build_invoice_context(db, inv, include_card_payment=False)["receipt_identity"] is None


def test_the_snapshot_records_when_it_was_frozen(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZRECEIPT When")
    invoice = _paid_deposit(db, booking)

    assert invoice.paid_to_account["frozen_at"] == invoice.paid_at.isoformat()
    assert invoice.paid_to_account["address"] == hamilton.address
