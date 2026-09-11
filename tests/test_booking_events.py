import datetime as dt

from app.models import BookingEvent
from app.models.booking import BookingStatus
from app.services.booking import change_status, create_booking


def _make_booking(db, space, **overrides):
    kwargs = dict(
        space_id=space.id,
        contact_id=None,
        event_date=dt.date(2026, 9, 12),
        start_time=dt.time(12, 0),
        end_time=dt.time(16, 0),
        event_name="Smith 50th",
        event_type="birthday",
        adult_count=50,
        child_count=0,
        notes=None,
        actor="aaron@meantime.com.au",
    )
    kwargs.update(overrides)
    return create_booking(db, **kwargs)


def test_create_booking_writes_a_created_event(db, loft):
    booking = _make_booking(db, loft)

    events = db.query(BookingEvent).filter_by(booking_id=booking.id).all()
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].new_value == "enquiry"
    assert events[0].actor == "aaron@meantime.com.au"


def test_status_change_appends_rather_than_overwrites(db, loft):
    booking = _make_booking(db, loft)

    change_status(db, booking, BookingStatus.tentative, actor="aaron@meantime.com.au")
    change_status(db, booking, BookingStatus.confirmed, actor="aaron@meantime.com.au")

    events = db.query(BookingEvent).filter_by(booking_id=booking.id).order_by(BookingEvent.created_at).all()
    assert [e.event_type for e in events] == ["created", "status_changed", "status_changed"]
    assert (events[1].old_value, events[1].new_value) == ("enquiry", "tentative")
    assert (events[2].old_value, events[2].new_value) == ("tentative", "confirmed")
    assert booking.status == BookingStatus.confirmed


def test_booking_events_cannot_be_updated(db, loft):
    booking = _make_booking(db, loft)
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).first()

    event.actor = "someone-else"
    try:
        db.flush()
        raised = False
    except Exception:
        raised = True
        db.rollback()

    assert raised, "the append-only trigger should have rejected this UPDATE"


def test_booking_events_cannot_be_deleted(db, loft):
    booking = _make_booking(db, loft)
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).first()

    db.delete(event)
    try:
        db.flush()
        raised = False
    except Exception:
        raised = True
        db.rollback()

    assert raised, "the append-only trigger should have rejected this DELETE"


# --- a booking carries its venue, and cannot lose or change it -------------
#
# Migration d8c3f1a7e920. Two invariants live in Postgres rather than in this
# service, so a hand-written UPDATE cannot go round them the way it could go
# round assign_space_and_time.


def test_a_new_booking_carries_its_venue(db, loft, hamilton):
    booking = _make_booking(db, loft)
    assert booking.venue_id == hamilton.id
    assert booking.venue_id == booking.space.venue_id


def test_a_bookings_venue_can_never_be_changed(db, loft, hamilton):
    """The trigger, not the service. assign_space_and_time already refuses a
    cross-venue move, but that is a property of one function; this is a
    property of the table. A hand-written UPDATE is the thing it stops."""
    import pytest as _pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    from app.models import Venue

    booking = _make_booking(db, loft)
    other = Venue(name="Meantime The Entrance", slug="entrance")
    db.add(other)
    db.flush()

    with _pytest.raises(DatabaseError, match="cannot move between venues"):
        db.execute(
            text("UPDATE bookings SET venue_id = :v WHERE id = :b"),
            {"v": str(other.id), "b": str(booking.id)},
        )
    db.rollback()


def test_a_bookings_venue_cannot_disagree_with_its_space(db, loft, hamilton):
    """The composite foreign key. Changing the space alone to another venue's
    room leaves venue_id saying something the space contradicts, and Postgres
    refuses the row rather than storing a booking whose two answers differ."""
    import pytest as _pytest
    from decimal import Decimal

    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    from app.models import Space, Venue

    booking = _make_booking(db, loft)
    other = Venue(name="Meantime The Entrance", slug="entrance")
    db.add(other)
    db.flush()
    deck = Space(
        venue_id=other.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(deck)
    db.flush()

    with _pytest.raises(DatabaseError):
        db.execute(
            text("UPDATE bookings SET space_id = :s WHERE id = :b"),
            {"s": str(deck.id), "b": str(booking.id)},
        )
    db.rollback()


def test_an_ordinary_update_still_works(db, loft, hamilton):
    """The trigger fires on every UPDATE to bookings, so prove it lets the
    ordinary ones through. A trigger that refuses everything would pass both
    tests above and break the app."""
    booking = _make_booking(db, loft)
    booking.event_name = "Renamed Party"
    db.flush()
    db.refresh(booking)
    assert booking.event_name == "Renamed Party"


def test_an_insert_that_omits_the_venue_is_filled_from_the_space(db, loft, hamilton):
    """The rollback net. venue_id is NOT NULL with no constant default, so
    redeploying the PREVIOUS build -- whose code does not set it -- would
    otherwise fail every insert, including the public enquiry form. Rolling
    back by redeploying the last build is how every earlier migration here
    was safe, and this keeps that true.

    It does not weaken the explicit-venue rule: create_booking always sets
    it (pinned above), and the composite FK still refuses anything that
    disagrees with the space."""
    import datetime as dt
    import uuid

    from sqlalchemy import text

    reference = f"PRB-{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        text(
            """
            INSERT INTO bookings (id, space_id, event_date, event_name, adult_count, child_count,
                                  status, reference_code, agreed_min_adults, agreed_min_food_spend,
                                  pricing_locked_at, created_at, updated_at)
            VALUES (:b, :s, :d, 'Old Build Insert', 10, 0, 'tentative', :r, 0, 0, now(), now(), now())
            """
        ),
        {"b": uuid.uuid4(), "s": loft.id, "d": dt.date(2027, 7, 7), "r": reference},
    )

    got = db.execute(
        text("SELECT venue_id FROM bookings WHERE reference_code = :r"), {"r": reference}
    ).scalar()
    assert got is not None, "an insert without a venue must not fail"
    assert str(got) == str(hamilton.id), "and it must be filled from the space, not guessed"
