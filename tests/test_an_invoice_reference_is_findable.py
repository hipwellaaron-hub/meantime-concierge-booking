"""A client quoting "HAM-1004" can be found.

An invoice reference now reads HAM-1004 -- the same shape as the booking
reference HAM-20261114-AB12C printed beside it on the page -- and the one
search box in the admin is labelled "Event name, reference, or contact". So
a client rings quoting an invoice, staff type it into the box the label
invites them to use, and get "No bookings match".

The lookup gap is older than the prefix: the bare integer was never
searchable either. What the prefix added is the INVITATION -- two
identifiers that rhyme, one of which the search understands.

It resolves to the BOOKING deliberately. That page is where every invoice
for the event already lives, so landing there answers "which invoice is
this?" and everything around it at once.
"""
import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.models import Space, Venue
from app.services import invoicing
from app.services.booking import create_booking, search_bookings

DUE = dt.date.today() + dt.timedelta(days=7)


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


def _booked_and_invoiced(db, venue, name):
    booking = create_booking(
        db, space_id=venue.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=50), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=DUE, actor="test")
    db.flush()
    return booking, invoice


def test_searching_an_invoice_reference_finds_its_booking(db, hamilton, loft):
    """THE one. Before this it returned nothing at all."""
    booking, invoice = _booked_and_invoiced(db, hamilton, f"ZZFIND {uuid.uuid4().hex[:6]}")

    found = search_bookings(db, venue_id=hamilton.id, query=invoice.invoice_reference)

    assert [b.id for b in found] == [booking.id], (
        f"searching {invoice.invoice_reference} found {[b.event_name for b in found]}"
    )


def test_it_is_case_insensitive_like_every_other_search_here(db, hamilton, loft):
    """Somebody reading a number off a phone screen types it lower case."""
    booking, invoice = _booked_and_invoiced(db, hamilton, f"ZZCASE {uuid.uuid4().hex[:6]}")

    found = search_bookings(db, venue_id=hamilton.id, query=invoice.invoice_reference.lower())

    assert [b.id for b in found] == [booking.id]


def test_a_booking_with_two_invoices_comes_back_once(db, hamilton, loft):
    """An EXISTS, not a join. A join would return the booking once per
    matching invoice, and a duplicated row in a search result is the kind
    of thing that gets read as two bookings."""
    booking, deposit = _booked_and_invoiced(db, hamilton, f"ZZTWO {uuid.uuid4().hex[:6]}")
    invoicing.create_final_invoice(
        db, booking, due_date=DUE, actor="test",
        line_items=[{"description": "Food", "quantity": 1, "unit_price": "500.00"}],
    )
    db.flush()

    found = search_bookings(db, venue_id=hamilton.id, query=deposit.invoice_reference)

    assert len(found) == 1, f"the booking came back {len(found)} times"


def test_the_search_is_still_scoped_to_the_venue(db, hamilton, loft, entrance):
    """Adding a new thing to match must not widen what a venue's page can
    see. An Entrance invoice is not findable from Hamilton's search."""
    _booking, ent_invoice = _booked_and_invoiced(db, entrance, "ZZENT Invoice")

    from_hamilton = search_bookings(db, venue_id=hamilton.id, query=ent_invoice.invoice_reference)
    from_entrance = search_bookings(db, venue_id=entrance.id, query=ent_invoice.invoice_reference)

    assert from_hamilton == [], "another venue's invoice was findable from this venue's search"
    assert len(from_entrance) == 1


def test_the_other_three_searches_still_work(db, hamilton, loft, contact):
    """The whole point of an `or_` is that it adds rather than replaces."""
    booking, _invoice = _booked_and_invoiced(db, hamilton, f"ZZNAME {uuid.uuid4().hex[:6]}")
    booking.contact_id = contact.id
    db.flush()

    by_name = search_bookings(db, venue_id=hamilton.id, query="ZZNAME")
    by_ref = search_bookings(db, venue_id=hamilton.id, query=booking.reference_code)
    by_contact = search_bookings(db, venue_id=hamilton.id, query=contact.name[:6])

    assert booking.id in [b.id for b in by_name]
    assert booking.id in [b.id for b in by_ref]
    assert booking.id in [b.id for b in by_contact]


def test_the_placeholder_says_an_invoice_reference_works():
    """The label is what invites somebody to type it. A search that matches
    something the label does not mention is a feature nobody uses."""
    import pathlib

    markup = pathlib.Path("app/templates/admin/bookings_list.html").read_text(encoding="utf-8")

    assert "invoice reference" in markup.lower(), (
        "the search box does not tell staff an invoice reference works"
    )
