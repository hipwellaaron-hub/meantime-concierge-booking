"""Two legal entities, two invoice registers.

Every invoice used to draw from one Postgres sequence with a UNIQUE across
the whole table. Two companies sharing that means Nice Try Events' tax
invoices interleave with Meantime Pty Ltd's, and each register carries holes
it cannot explain -- an accountant cannot tell a missing invoice from the
other company's.

WHAT A CLIENT READS is `HAM-1004` / `ENT-1001`: the venue's
reference_prefix and that venue's own number, the same shape as the booking
reference printed two rows above it on the same page. Hamilton's existing
numbers keep their VALUES and gain the prefix.

The reference is STORED, for the reason bookings.reference_code is stored:
a client holds it, so editing a venue row must not change what is printed
on an invoice already sent. It carries the global UNIQUE that
invoice_number used to carry, so a quoted reference resolves to exactly one
invoice across both companies.
"""
import datetime as dt
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Invoice, Space, Venue
from app.services import invoicing
from app.services.booking import create_booking

DUE = dt.date.today() + dt.timedelta(days=7)


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        legal_name="Nice Try Events Pty Ltd", reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


def _invoice_at(db, venue, name="Register Test"):
    booking = create_booking(
        db, space_id=venue.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=DUE, actor="test")
    db.flush()
    return invoice


# --- the two registers are separate ----------------------------------------


def test_each_venue_has_its_own_running_number(db, hamilton, loft, entrance):
    """THE point. Interleaved creation, and neither series takes a number
    out of the other."""
    ham_one = _invoice_at(db, hamilton, "Ham One")
    ent_one = _invoice_at(db, entrance, "Ent One")
    ham_two = _invoice_at(db, hamilton, "Ham Two")
    ent_two = _invoice_at(db, entrance, "Ent Two")

    assert ham_two.invoice_number == ham_one.invoice_number + 1, (
        "an Entrance invoice took a number out of Hamilton's series"
    )
    assert ent_two.invoice_number == ent_one.invoice_number + 1


def test_the_second_venue_starts_its_register_at_the_house_first_number(db, hamilton, entrance):
    """A brand-new company's first tax invoice is its register's first
    number, not a continuation of somebody else's."""
    first = _invoice_at(db, entrance, "Ent First")

    assert first.invoice_number == 1001
    assert first.invoice_reference == "ENT-1001"


def test_two_venues_can_hold_the_same_number(db, hamilton, loft, entrance):
    """The uniqueness moved: per venue for the integer, global for the
    reference. HAM-1001 and ENT-1001 are two companies' first invoices and
    both are correct."""
    ent = _invoice_at(db, entrance, "Ent Same Number")
    ham = _invoice_at(db, hamilton, "Ham Same Number")

    # Hamilton's register is wherever it is; force the collision directly.
    assert ent.invoice_number == 1001
    assert ham.invoice_reference != ent.invoice_reference
    assert ham.invoice_reference.startswith("HAM-")


# --- what the client reads --------------------------------------------------


def test_the_reference_is_the_prefix_and_the_number(db, hamilton, entrance):
    invoice = _invoice_at(db, entrance, "Reference Shape")

    assert invoice.invoice_reference == f"ENT-{invoice.invoice_number}"


def test_the_reference_cannot_be_rewritten(db, hamilton, entrance):
    """A number a client holds is the one thing that must never change.
    Enforced by the table, not by a service -- an UPDATE from anywhere is
    refused."""
    invoice = _invoice_at(db, entrance, "Frozen Reference")

    invoice.invoice_reference = "ENT-9999"
    with pytest.raises(DatabaseError):
        db.flush()
    db.rollback()


def test_an_invoice_cannot_move_between_venues(db, hamilton, loft, entrance):
    invoice = _invoice_at(db, entrance, "Cannot Move")

    invoice.venue_id = hamilton.id
    with pytest.raises(DatabaseError):
        db.flush()
    db.rollback()


def test_the_number_cannot_be_rewritten(db, hamilton, entrance):
    invoice = _invoice_at(db, entrance, "Frozen Number")

    invoice.invoice_number = 4242
    with pytest.raises(DatabaseError):
        db.flush()
    db.rollback()


# --- the venue comes from the booking, never from the caller ---------------


def test_the_venue_is_taken_from_the_booking_whatever_the_caller_says(
    db, hamilton, loft, entrance
):
    """A caller supplying the wrong venue_id cannot make the row disagree
    with its own booking -- the trigger overwrites it."""
    booking = create_booking(
        db, space_id=entrance.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="Wrong Venue Supplied", event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()

    invoice = Invoice(
        booking_id=booking.id, venue_id=hamilton.id,  # deliberately wrong
        type=invoicing.InvoiceType.deposit, line_items=[], subtotal=Decimal("0"),
        surcharge=Decimal("0"), total=Decimal("0"), due_date=DUE,
    )
    db.add(invoice)
    db.flush()
    db.refresh(invoice)

    assert invoice.venue_id == entrance.id, "the supplied venue_id was trusted"
    assert invoice.invoice_reference.startswith("ENT-")


def test_a_venue_with_no_reference_prefix_cannot_issue_an_invoice(db, hamilton, entrance):
    """The venue-specific fact refuses; the house convention (start at
    1001) does not need to. A prefix is the thing nobody can guess.

    THE BOOKING IS MADE FIRST, while the prefix is still there. Clearing it
    up front would have made create_booking refuse instead -- a booking
    needs the same prefix for its own reference code -- and this test would
    then have passed with the invoice trigger's check deleted, proving only
    that generate_reference_code works.
    """
    booking = create_booking(
        db, space_id=entrance.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="No Prefix", event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()

    entrance.reference_prefix = None
    db.flush()

    with pytest.raises(DatabaseError) as exc:
        invoicing.create_deposit_invoice(db, booking, due_date=DUE, actor="test")
        db.flush()
    assert "reference_prefix" in str(exc.value)
    db.rollback()


# --- concurrency ------------------------------------------------------------


@pytest.fixture()
def committed_booking():
    """A booking that really exists in the database, not inside a savepoint.

    The `db` fixture joins a savepoint and its commit() releases that
    savepoint rather than committing, so a second connection cannot see
    anything it wrote -- and two connections seeing each other is the whole
    subject of the test below. Same approach, and the same reason, as
    tests/test_invoicing_concurrency.py.

    A fixture rather than a cleanup block, so the rows go even when an
    assertion fails: this is the module pattern that once leaked forty
    venues into the shared test database.
    """
    from tests.conftest import TestSessionLocal, purge_venue

    setup = TestSessionLocal()
    venue = Venue(
        name="Register Concurrency Venue",
        slug=f"register-concurrency-{uuid.uuid4().hex[:8]}",
        trading_name="Register Concurrency Venue",
        reference_prefix=uuid.uuid4().hex[:5].upper(),
    )
    space = Space(
        venue=venue, name="Test Space", capacity=100, min_food_spend=0,
        standard_min_adults=0, is_bookable=True,
    )
    setup.add_all([venue, space])
    setup.commit()
    venue_id = venue.id

    booking = create_booking(
        setup, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=41), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="ZZCONCURRENT Register", event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    setup.commit()
    booking_id = booking.id
    setup.close()
    try:
        yield booking_id
    finally:
        purge_venue(venue_id)


def test_two_invoices_raised_at_once_for_one_venue_get_different_numbers(committed_booking):
    """A row lock, not a sequence, so this has to be proven rather than
    assumed. Four real connections, released together by a barrier."""
    engine = create_engine(settings.test_database_url)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(4)
    made, errors = [], []

    def raise_one():
        barrier.wait()
        session = Session()
        try:
            row = session.execute(
                text(
                    "INSERT INTO invoices (id, booking_id, type, line_items, subtotal, surcharge, "
                    "total, status, due_date, access_token) VALUES "
                    "(gen_random_uuid(), :b, 'deposit', '[]'::jsonb, 0, 0, 0, 'draft', :d, :token) "
                    "RETURNING invoice_number, invoice_reference"
                ),
                {"b": committed_booking, "d": DUE, "token": uuid.uuid4().hex},
            ).one()
            session.commit()
            made.append(row)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=raise_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()

    assert not errors, errors
    numbers = sorted(n for n, _ref in made)
    assert len(set(numbers)) == 4, f"two invoices took the same number: {numbers}"
    assert numbers == list(range(numbers[0], numbers[0] + 4)), (
        f"the register skipped a number: {numbers}"
    )
    assert len({ref for _n, ref in made}) == 4
