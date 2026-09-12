"""What each client-facing document says the venue IS.

Written BEFORE the venue-identity source moves off module constants, which
is the order the 2026-09-11 review asked for: rewriting all ten
policy.VENUE_*/BANK_* constants to a fictional venue produced 27 failures
and ZERO of them were Event Order renders. The suite could not see the one
surface that matters most, so a wrong-entity run sheet would have shipped
silently.

These pin what a CLIENT actually receives, by rendering the real routes,
not by reading a constant back. They are deliberately literal: the values
appear nowhere but policy.py today, so a change that moves them without
preserving them fails here rather than in front of somebody.

Three surfaces, three different mechanisms, and the difference is the
point:
  * the AGREEMENT freezes identity into document.content at generation
    time -- a contract says what was true when it was agreed;
  * the INVOICE reads live values -- an unpaid invoice must point at the
    account that is current now, not the one that was current then;
  * the EVENT ORDER reads live values too, and is the surface with no
    cover at all until this file.
"""
import datetime as dt

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.services.invoicing import create_deposit_invoice, mark_sent as mark_invoice_sent

# Hamilton's, today. Not imported from policy on purpose: importing the
# constant would make these tests pass no matter what the constant said,
# which is exactly the hole they exist to close.
TRADING_NAME = "Meantime Hamilton"
LEGAL_NAME = "Meantime Pty Ltd"
ABN = "36 654 270 532"
ADDRESS = "104 Beaumont St, Hamilton NSW 2303"
PHONE = "(02) 40410697"
BSB = "063-519"
ACCOUNT_NUMBER = "10315591"


def _booking(db, space, contact, *, name="Identity Test"):
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 8, 21),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# --- the Event Order: the surface that had no cover ------------------------


def test_the_event_order_a_client_opens_names_the_venue(db, loft, contact, hamilton):
    booking = _booking(db, loft, contact)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")

    try:
        resp = _client(db).get(f"/d/{document.access_token}")
        assert resp.status_code == 200
        # The SCREEN version carries the header line only. The ABN and
        # address are in the PDF footer and nowhere on screen -- checked,
        # not assumed: asserting them here failed, which is how I learnt
        # the two surfaces print different things.
        for value in (TRADING_NAME, PHONE):
            assert value in resp.text, f"the Event Order no longer prints {value!r}"
    finally:
        app.dependency_overrides.clear()


def test_the_event_order_pdf_names_the_venue(db, loft, contact, hamilton):
    """The PDF path renders through get_template().render() and skips the
    request context entirely, so it can drift from the screen version."""
    booking = _booking(db, loft, contact)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")

    from app.templating import templates

    html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )
    for value in (TRADING_NAME, ABN, ADDRESS):
        assert value in html, f"the Event Order PDF no longer prints {value!r}"


# --- the invoice: live by design ------------------------------------------


def test_the_invoice_a_client_opens_names_the_venue_and_the_account(db, loft, contact, hamilton):
    booking = _booking(db, loft, contact)
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2027, 8, 1), actor="test")
    mark_invoice_sent(db, invoice, actor="test")

    try:
        resp = _client(db).get(f"/i/{invoice.access_token}")
        assert resp.status_code == 200
        for value in (TRADING_NAME, ABN, ADDRESS, BSB, ACCOUNT_NUMBER):
            assert value in resp.text, f"the invoice no longer prints {value!r}"
    finally:
        app.dependency_overrides.clear()


# --- the agreement: frozen by design --------------------------------------


def test_the_agreement_freezes_the_venue_into_its_own_content(db, loft, contact, hamilton):
    """A contract says what was true when it was agreed, so these live in
    the document rather than being read live at render time. That is the
    one surface already doing the right thing."""
    booking = _booking(db, loft, contact)
    content = generate_agreement_content(booking)

    assert content["venue_abn"] == ABN
    assert content["venue_address"] == ADDRESS
    assert content["venue"] == TRADING_NAME

    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, content, actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")
    try:
        resp = _client(db).get(f"/d/{document.access_token}")
        assert resp.status_code == 200
        for value in (TRADING_NAME, ABN, ADDRESS):
            assert value in resp.text, f"the agreement no longer prints {value!r}"
    finally:
        app.dependency_overrides.clear()


def test_a_frozen_agreement_does_not_follow_a_later_change(db, loft, contact, hamilton):
    """The other half of 'frozen': prove it by changing the source and
    re-rendering. Without this the test above passes whether the value is
    frozen or live."""
    from unittest.mock import patch

    from app.services import document_generation

    booking = _booking(db, loft, contact)
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")

    from app.templating import templates

    with patch.object(document_generation.policy, "VENUE_ABN", "99 999 999 999"):
        html = templates.get_template("document.html").render(
            document=document, booking=booking, is_pdf=True
        )

    assert ABN in html, "a signed agreement must keep the ABN it was agreed under"
    assert "99 999 999 999" not in html


# --- the venue record itself ----------------------------------------------


def test_the_seeded_venue_carries_its_own_identity(db, hamilton):
    """Migration e1b6a44c7f83 gave the venue record the identity that used
    to live only in module constants. Nothing renders from these columns
    yet; this pins that they are POPULATED, because a freshly seeded
    database and a migrated one disagreeing is the kind of difference that
    only shows up on somebody's invoice."""
    assert hamilton.trading_name == TRADING_NAME
    assert hamilton.legal_name == LEGAL_NAME
    assert hamilton.abn == ABN
    assert hamilton.address == ADDRESS
    assert hamilton.phone == PHONE
    assert hamilton.bank_bsb == BSB
    assert hamilton.bank_account_number == ACCOUNT_NUMBER
    assert hamilton.reference_prefix == "HAM"
    # The NAME of the variable, never the key.
    assert hamilton.stripe_secret_key_env == "STRIPE_SECRET_KEY"
    assert not any(
        str(v or "").startswith("sk_") for v in vars(hamilton).values()
    ), "no Stripe secret may ever be stored on the venue row"


def test_the_internal_label_and_the_trading_name_stay_different(db, hamilton):
    """`name` is the internal label and has been since the first import.
    Printing it to a client would say "Hamilton" where the contract says
    "Meantime Hamilton"."""
    assert hamilton.name == "Hamilton"
    assert hamilton.trading_name == "Meantime Hamilton"
    assert hamilton.name != hamilton.trading_name
