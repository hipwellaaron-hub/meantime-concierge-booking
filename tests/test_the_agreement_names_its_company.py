"""A hire agreement names the company it binds.

A contract has two parties. This one named one: the hirer signed a blank
line and nothing anywhere on the page said who they were contracting WITH.
The header printed a trading name and an ABN -- and an ABN identifies a
legal ENTITY, so printing the number without the entity hands a client the
half they cannot resolve backwards.

`generate_agreement_content` freezes eighteen keys and `legal_name` was not
among them. `app/templating.py` venue_identity has supplied
`venue_legal_name` all along and no client template read it, which
`tests/test_identity_keys_are_supplied.py` cannot catch: it checks
template-to-dict and never dict-to-template, so a key that is supplied and
never printed is invisible to it.

FROZEN, not read live, like the other five venue facts on this document. A
signed contract reflects what was agreed; the entity that signed it does not
change because somebody edited a row afterwards.
"""
import datetime as dt
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Space, Venue
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content


@pytest.fixture()
def nice_try(db, hamilton):
    """A second venue with a DIFFERENT legal entity from its trading name --
    which is the whole point: 'Meantime The Entrance' is not a company."""
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        legal_name="Nice Try Events Pty Ltd", abn="28 647 750 892",
        address="The Entrance NSW", contact_name="Ruby",
        contact_email="hello@nicetry.example", reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    space = Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(space)
    db.flush()
    return venue


def _booking_at(db, space, name="Company Named"):
    return create_booking(
        db, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=45), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="test",
    )


# --- what is frozen --------------------------------------------------------


def test_the_agreement_freezes_the_contracting_entity(db, hamilton, nice_try):
    booking = _booking_at(db, nice_try.spaces[0])
    db.flush()

    content = generate_agreement_content(booking)

    assert content["venue_legal_name"] == "Nice Try Events Pty Ltd"
    assert content["venue_abn"] == "28 647 750 892"
    assert content["venue"] == "Meantime The Entrance", "the trading name is still the header"


def test_a_venue_with_no_legal_name_freezes_a_blank_not_another_company(
    db, hamilton, nice_try
):
    """The rule this whole file of columns is built on: blank is the honest
    answer to 'nobody has said', and a fallback is how one company's name
    reaches the other's contract."""
    nice_try.legal_name = None
    db.flush()
    booking = _booking_at(db, nice_try.spaces[0], name="No Legal Name")
    db.flush()

    content = generate_agreement_content(booking)

    assert content["venue_legal_name"] == ""
    assert "Meantime Pty Ltd" not in str(content)


def test_hamiltons_agreement_still_names_meantime_pty_ltd(db, hamilton, loft):
    """The other direction, so a change that froze nothing could not pass
    the test above on its own."""
    booking = _booking_at(db, loft, name="Hamilton Agreement")
    db.flush()

    content = generate_agreement_content(booking)

    assert content["venue_legal_name"] == hamilton.legal_name
    assert content["venue_legal_name"], "Hamilton's own legal name went missing"


# --- the Deposits clause ---------------------------------------------------


def test_the_deposits_clause_names_this_venue(db, hamilton, nice_try):
    """It read 'Meantime cannot guarantee any booking without a deposit' --
    a module constant, and the only self-naming word in the whole contract.
    On a Nice Try Events agreement it named neither that company nor that
    venue."""
    booking = _booking_at(db, nice_try.spaces[0], name="Deposits Clause")
    db.flush()

    deposits = next(
        s for s in generate_agreement_content(booking)["terms_sections"]
        if s["heading"] == "Deposits"
    )

    assert "Meantime The Entrance cannot guarantee" in deposits["body"]
    assert "Meantime cannot guarantee" not in deposits["body"]


def test_a_venue_with_no_trading_name_says_the_venue_not_another_name(db, hamilton, nice_try):
    nice_try.trading_name = None
    db.flush()
    booking = _booking_at(db, nice_try.spaces[0], name="No Trading Name")
    db.flush()

    deposits = next(
        s for s in generate_agreement_content(booking)["terms_sections"]
        if s["heading"] == "Deposits"
    )

    assert "The venue cannot guarantee" in deposits["body"]
    assert "Meantime" not in deposits["body"]


# --- what the client actually reads ----------------------------------------


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_the_rendered_agreement_names_the_company_it_binds(db, hamilton, nice_try, contact):
    """Freezing a key nothing prints is the bug this replaces, so the
    assertion is on the RENDERED page, not on the dict."""
    booking = _booking_at(db, nice_try.spaces[0], name="Rendered Agreement")
    booking.contact_id = contact.id  # mark_sent requires a real address
    db.flush()
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    documents_service.mark_sent(db, document, actor="test")
    db.flush()

    try:
        resp = _client(db).get(f"/d/{document.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert "Nice Try Events Pty Ltd" in resp.text, (
        "the agreement does not name the company the client is contracting with"
    )
    assert "in an agreement with Nice Try Events Pty Ltd" in resp.text, (
        "the form the client actually signs still does not name the other party"
    )
    assert "Meantime Pty Ltd" not in resp.text


def test_the_sweep_no_client_document_template_hardcodes_a_company():
    """A company name typed into a document template is right for one legal
    entity and wrong for the other, on the one artefact that binds them."""
    import pathlib
    import re

    offenders = []
    for name in ("document.html", "invoice.html"):
        source = pathlib.Path(f"app/templates/{name}").read_text(encoding="utf-8")
        stripped = source
        while "{#" in stripped and "#}" in stripped:
            start = stripped.index("{#")
            stripped = stripped[:start] + stripped[stripped.index("#}", start) + 2:]
        for company in ("Meantime Pty Ltd", "Nice Try Events"):
            if company in stripped:
                offenders.append(f"{name} names {company}")
        if re.search(r"ABN\s+\d", stripped):
            offenders.append(f"{name} has a literal ABN")

    assert not offenders, offenders
