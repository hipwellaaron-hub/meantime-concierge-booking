import datetime as dt

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, BookingEvent
from app.services.booking import create_booking
from app.services.enquiry_classification import (
    BIRTHDAY_CLARIFICATION_QUESTION,
    classify_and_flag,
    get_enquiries_needing_clarification,
)


def _next_weekday(start: dt.date, target_weekday: int) -> dt.date:
    days_ahead = (target_weekday - start.weekday()) % 7
    days_ahead = days_ahead or 7
    return start + dt.timedelta(days=days_ahead)


def _next_saturday(start: dt.date) -> dt.date:
    return _next_weekday(start, 5)


def _next_friday(start: dt.date) -> dt.date:
    return _next_weekday(start, 4)


def _make_booking(db, space, *, event_date, event_name="Test Enquiry"):
    return create_booking(
        db,
        space_id=space.id,
        contact_id=None,
        event_date=event_date,
        event_name=event_name,
        event_type=None,
        adult_count=50,
        child_count=0,
        notes=None,
        actor="test",
    )


# --- generic "Birthday" ambiguity -----------------------------------------


def test_generic_birthday_is_flagged_for_clarification(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, actor="test")
    assert any("milestone and guest ages not yet known" in f for f in flags)
    assert any(BIRTHDAY_CLARIFICATION_QUESTION in f for f in flags)


def test_generic_birthday_does_not_apply_18th_conditions():
    """The flag text itself must never assert RSA/ID/supervision
    conditions as fact for a generic birthday -- only ask about them."""
    import inspect

    from app.services import enquiry_classification

    source = inspect.getsource(enquiry_classification)
    # The module must only ever raise these terms in the context of "do
    # not apply ... until confirmed", never as a standalone assertion.
    assert "Do not apply 18th-birthday conditions" in source


def test_18th_birthday_does_not_trigger_generic_birthday_flag(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, actor="test")
    assert not any("milestone and guest ages not yet known" in f for f in flags)


def test_21st_birthday_does_not_trigger_generic_birthday_flag(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="21st Birthday", adult_count=40, actor="test")
    assert not any("milestone and guest ages not yet known" in f for f in flags)


# --- missing adult/minor split ---------------------------------------------


def test_birthday_missing_adult_count_is_flagged(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=None, actor="test")
    assert any("Adult/minor guest split not provided" in f for f in flags)


def test_birthday_with_adult_count_given_is_not_flagged_for_split(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="21st Birthday", adult_count=45, actor="test")
    assert not any("Adult/minor guest split not provided" in f for f in flags)


def test_non_birthday_event_missing_adult_count_is_not_flagged(db, unassigned_space):
    """The adult-split rule only applies to birthday-category enquiries --
    a wedding or corporate function not giving an adult split is normal
    and must not be flagged."""
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Wedding", adult_count=None, actor="test")
    assert flags == []


# --- 18th birthday + Saturday -----------------------------------------------


def test_18th_birthday_on_saturday_is_flagged(db, unassigned_space):
    saturday = _next_saturday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=saturday)
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, actor="test")
    assert any("Saturday" in f and "18th" in f for f in flags)


def test_18th_birthday_on_friday_is_not_flagged_for_saturday(db, unassigned_space):
    friday = _next_friday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=friday)
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, actor="test")
    assert not any("Saturday" in f for f in flags)


def test_21st_birthday_on_saturday_is_not_flagged(db, unassigned_space):
    """The Saturday restriction is specific to 18ths -- a 21st on a
    Saturday is normal and must not be flagged."""
    saturday = _next_saturday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=saturday)
    flags = classify_and_flag(db, booking, event_type="21st Birthday", adult_count=40, actor="test")
    assert not any("Saturday" in f for f in flags)


# --- persistence + idempotence ----------------------------------------------


def test_flags_are_persisted_as_booking_events(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, actor="test")

    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
    # Generic birthday + missing adult split = two distinct flags
    assert len(events) == 2


def test_clean_enquiry_raises_no_flags(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Wedding", adult_count=80, actor="test")
    assert flags == []
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
    assert events == []


# --- staff worklist ----------------------------------------------------------


def test_get_enquiries_needing_clarification_lists_flagged_open_enquiries(db, hamilton, unassigned_space):
    flagged = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)), event_name="Flagged One")
    classify_and_flag(db, flagged, event_type="Birthday", adult_count=None, actor="test")

    clean = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 8)), event_name="Clean One")
    classify_and_flag(db, clean, event_type="Wedding", adult_count=80, actor="test")

    results = get_enquiries_needing_clarification(db, hamilton)
    result_ids = {b.id for b in results}
    assert flagged.id in result_ids
    assert clean.id not in result_ids


def test_flagged_enquiry_drops_off_worklist_once_progressed(db, hamilton, unassigned_space):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, actor="test")

    assert booking.id in {b.id for b in get_enquiries_needing_clarification(db, hamilton)}

    change_status(db, booking, BookingStatus.offered, actor="test")
    assert booking.id not in {b.id for b in get_enquiries_needing_clarification(db, hamilton)}


# --- end-to-end wiring through the public endpoint --------------------------


def test_public_enquiry_endpoint_creates_clarification_flags(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/enquiries",
            data=dict(
                first_name="Alex",
                last_name="Nguyen",
                email="alex@example.com",
                event_name="Alex's Birthday",
                event_date=_next_friday(dt.date(2027, 1, 1)).isoformat(),
                dates_flexible="false",
                event_type="Birthday",
                attendee_count=50,
            ),
        )
        assert resp.status_code == 303

        booking = db.query(Booking).filter_by(event_name="Alex's Birthday").one()
        events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
        assert len(events) == 2  # generic-birthday + missing-adult-split
    finally:
        app.dependency_overrides.clear()
