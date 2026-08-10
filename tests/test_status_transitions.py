import datetime as dt

import pytest

from app.models import BookingEvent
from app.models.booking import BookingStatus
from app.services.booking import LEGAL_TRANSITIONS, create_booking, transition_status


def _make_booking(db, space, **overrides):
    kwargs = dict(
        space_id=space.id,
        contact_id=None,
        event_date=dt.date(2026, 9, 12),
        start_time=dt.time(12, 0),
        end_time=dt.time(16, 0),
        event_name="Transition Test",
        event_type="wedding",
        adult_count=50,
        child_count=0,
        notes=None,
        actor="test",
    )
    kwargs.update(overrides)
    return create_booking(db, **kwargs)


# --- legal transitions ------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (BookingStatus.enquiry, BookingStatus.offered),
        (BookingStatus.enquiry, BookingStatus.dead),
        (BookingStatus.offered, BookingStatus.tentative),
        (BookingStatus.offered, BookingStatus.dead),
        (BookingStatus.tentative, BookingStatus.confirmed),
        (BookingStatus.tentative, BookingStatus.cancelled),
        (BookingStatus.confirmed, BookingStatus.completed),
        (BookingStatus.confirmed, BookingStatus.cancelled),
    ],
)
def test_legal_transition_succeeds(db, loft, from_status, to_status):
    booking = _make_booking(db, loft, status=from_status)
    transition_status(db, booking, to_status, actor="test")
    assert booking.status == to_status


# --- illegal transitions -----------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (BookingStatus.enquiry, BookingStatus.confirmed),  # skips offered/tentative
        (BookingStatus.enquiry, BookingStatus.completed),
        (BookingStatus.enquiry, BookingStatus.cancelled),  # dead, not cancelled, is the enquiry-stage terminal
        (BookingStatus.offered, BookingStatus.confirmed),  # skips tentative
        (BookingStatus.tentative, BookingStatus.dead),  # dead only applies pre-tentative
        (BookingStatus.confirmed, BookingStatus.dead),
        (BookingStatus.confirmed, BookingStatus.tentative),  # no going backwards
    ],
)
def test_illegal_transition_is_rejected(db, loft, from_status, to_status):
    booking = _make_booking(db, loft, status=from_status)
    with pytest.raises(ValueError):
        transition_status(db, booking, to_status, actor="test")
    assert booking.status == from_status


@pytest.mark.parametrize("terminal_status", [BookingStatus.completed, BookingStatus.cancelled, BookingStatus.dead])
def test_terminal_statuses_have_no_legal_next_status(terminal_status):
    assert LEGAL_TRANSITIONS[terminal_status] == ()


def test_terminal_status_rejects_every_transition(db, loft):
    booking = _make_booking(db, loft, status=BookingStatus.cancelled)
    with pytest.raises(ValueError):
        transition_status(db, booking, BookingStatus.confirmed, actor="test")


# --- reason is recorded, not required ----------------------------------------


def test_transition_without_reason_succeeds(db, loft):
    booking = _make_booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="test")
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, field_name="status_change_reason").all()
    assert events == []


def test_transition_with_reason_is_recorded_as_a_separate_event(db, loft):
    booking = _make_booking(db, loft)
    transition_status(db, booking, BookingStatus.offered, actor="test", reason="quoted, waiting on deposit")

    status_events = db.query(BookingEvent).filter_by(booking_id=booking.id, field_name="status").all()
    assert len(status_events) == 1
    assert (status_events[0].old_value, status_events[0].new_value) == ("enquiry", "offered")

    reason_events = db.query(BookingEvent).filter_by(booking_id=booking.id, field_name="status_change_reason").all()
    assert len(reason_events) == 1
    assert reason_events[0].new_value == "quoted, waiting on deposit"
    assert reason_events[0].actor == "test"


def test_same_status_transition_is_a_no_op(db, loft):
    booking = _make_booking(db, loft, status=BookingStatus.offered)
    transition_status(db, booking, BookingStatus.offered, actor="test")
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, field_name="status").all()
    assert events == []
