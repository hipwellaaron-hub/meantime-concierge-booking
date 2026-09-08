"""An invoice's Payment Terms box states THAT invoice's term.

The box used to loop over every invoice on the booking and print a line
each, so a final invoice replayed the deposit's term beside its own. On
HAM-20260912-2R11Q that read "Deposit Due on 21-03-2026" on an invoice
issued in September, months after that deposit was paid -- next to the
balance term, on the client's own copy and on the PDF.

The draft case was worse. The loop's filter drops draft invoices, which
includes the very invoice being previewed, so a staff preview of a draft
final invoice showed only the stale deposit line and no balance term at
all.

Narrowing it loses nothing: the deposit is still on the page in Related
Invoices, in the Received total, and as the "less deposit credited" line
item.

Not covered here, deliberately: the balance due date itself. A final
invoice's due date is currently the event date, which is a separate defect
in app/services/wizard_generation.py and needs Aaron's rule before it can
be written down.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services.booking import create_booking

DEPOSIT_DUE = dt.date(2026, 3, 21)
BALANCE_DUE = dt.date(2027, 5, 7)


def _booking(db, space, name="Invoice Terms"):
    contact = Contact(name="Terms Client", email=f"terms.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _paid_deposit(db, booking):
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        DEPOSIT_DUE, actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=500, method=PaymentMethod.card, actor="test")
    return invoice


def _final(db, booking, *, send=True):
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.final,
        [{"description": "Balance", "quantity": 1, "unit_price": "1585.00"}],
        BALANCE_DUE, actor="test",
    )
    if send:
        invoicing.mark_sent(db, invoice, actor="test")
    return invoice


def _terms(html):
    """Just the Payment Terms box."""
    start = html.index("Payment Terms")
    return html[start:html.index("</div>", html.index("</div>", start) + 1) + 6]


def _client_view(client, invoice):
    page = client.get(f"/i/{invoice.access_token}")
    assert page.status_code == 200, page.status_code
    return page.text


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


def test_a_final_invoice_does_not_replay_a_settled_deposit(client, db, loft):
    booking = _booking(db, loft, "Final Terms")
    _paid_deposit(db, booking)
    final = _final(db, booking)

    terms = _terms(_client_view(client, final))

    assert "Deposit" not in terms, f"the settled deposit's term is still on the final invoice: {terms}"
    assert "21-03-2026" not in terms, "a due date from months ago"
    assert "Balance Due on 07-05-2027" in terms


def test_a_deposit_invoice_still_says_deposit(client, db, loft):
    """The wording is right on a deposit -- that was never the bug."""
    booking = _booking(db, loft, "Deposit Terms")
    deposit = _paid_deposit(db, booking)

    terms = _terms(_client_view(client, deposit))

    assert "Deposit Due on 21-03-2026" in terms
    assert "Balance" not in terms


def test_a_draft_final_invoice_shows_its_own_term(admin_client, db, loft):
    """The worst case. The old loop filtered out drafts, which included the
    invoice being previewed, so staff saw the deposit line and no balance
    line at all."""
    booking = _booking(db, loft, "Draft Terms")
    _paid_deposit(db, booking)
    draft = _final(db, booking, send=False)

    page = admin_client.get(f"/admin/bookings/{booking.id}/invoices/{draft.id}/preview")
    assert page.status_code == 200, page.status_code
    terms = _terms(page.text)

    assert "Balance Due on 07-05-2027" in terms
    assert "Deposit" not in terms


def test_exactly_one_term_is_printed(client, db, loft):
    """One invoice, one term. The count is the property that broke."""
    booking = _booking(db, loft, "One Term")
    _paid_deposit(db, booking)
    final = _final(db, booking)

    terms = _terms(_client_view(client, final))

    assert len(re.findall(r"Due on", terms)) == 1, terms


def test_the_deposit_is_still_visible_elsewhere_on_the_page(client, db, loft):
    """Narrowing the box must not hide the deposit from the client."""
    booking = _booking(db, loft, "Still Shown")
    _paid_deposit(db, booking)
    final = _final(db, booking)

    html = _client_view(client, final)

    assert "Deposit" in html, "the deposit vanished from the invoice entirely"
    assert "Deposit" not in _terms(html)
