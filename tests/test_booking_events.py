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
