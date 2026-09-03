import datetime as dt

from fastapi.testclient import TestClient

from app.main import app
from app.database import get_db
from app.models.booking import BookingStatus
from app.services.availability import get_space_candidates, is_space_free
from app.services.booking import create_booking

EVENT_DATE = dt.date(2026, 9, 12)  # a Saturday


def _book(db, space, start, end, **overrides):
    # Defaults to 'tentative' (a real hold) rather than 'enquiry', since
    # enquiries/offers deliberately don't block the space -- see
    # BLOCKING_STATUSES in app/models/booking.py.
    kwargs = dict(
        space_id=space.id,
        contact_id=None,
        event_date=EVENT_DATE,
        start_time=start,
        end_time=end,
        event_name="Test Event",
        event_type="party",
        adult_count=10,
        child_count=0,
        notes=None,
        actor="test",
        status=BookingStatus.tentative,
    )
    kwargs.update(overrides)
    return create_booking(db, **kwargs)


def test_is_space_free_true_with_no_bookings(db, loft):
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_is_space_free_false_once_booked(db, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0))
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is False
    assert len(blocking) == 1


def test_enquiry_status_does_not_block_the_space(db, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry)
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_multiple_enquiries_can_coexist_for_the_same_slot(db, loft):
    """The whole point of lead capture: two people enquiring about the same
    date shouldn't stop the second enquiry from even being logged."""
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry, event_name="Enquiry A")
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry, event_name="Enquiry B")

    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True  # neither has actually secured the space yet


def test_cancelled_booking_does_not_block(db, loft):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _book(db, loft, dt.time(12, 0), dt.time(16, 0))
    change_status(db, booking, BookingStatus.cancelled, actor="test")

    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_get_space_candidates_excludes_too_small(db, hamilton, lounge):
    # Lounge caps at 35; ask for 60 guests.
    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(12, 0), dt.time(16, 0), guest_count=60)
    lounge_result = next(c for c in candidates if c["space"].id == lounge.id)
    assert lounge_result["is_available"] is False
    assert "too_small" in lounge_result["reasons"]


def test_get_space_candidates_excludes_already_booked(db, hamilton, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0))

    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(14, 0), dt.time(18, 0), guest_count=10)
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    assert loft_result["is_available"] is False
    assert "already_booked" in loft_result["reasons"]


def test_get_space_candidates_non_overlapping_time_is_available(db, hamilton, loft):
    _book(db, loft, dt.time(9, 0), dt.time(12, 0))

    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(13, 0), dt.time(17, 0), guest_count=10)
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    assert loft_result["is_available"] is True
    assert loft_result["reasons"] == []


def test_wheelchair_filter_excludes_inaccessible_spaces(db, hamilton, loft, lounge):
    candidates = get_space_candidates(
        db, hamilton.id, EVENT_DATE, dt.time(12, 0), dt.time(16, 0), guest_count=10, require_wheelchair_accessible=True
    )
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    lounge_result = next(c for c in candidates if c["space"].id == lounge.id)
    assert "not_accessible" in loft_result["reasons"]
    assert lounge_result["is_available"] is True


def test_spaces_endpoint_returns_warnings_for_saturday_daytime_overrun(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/availability/spaces",
            params={"date": str(EVENT_DATE), "start": "11:00:00", "end": "18:00:00", "guests": 20},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert any("4:30pm" in w for w in body["warnings"])
        assert len(body["spaces"]) == 3
    finally:
        app.dependency_overrides.clear()


def test_availability_endpoint_reports_free_space(db, hamilton, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(loft.id)})
        assert resp.status_code == 200
        assert resp.json()["is_free"] is True
    finally:
        app.dependency_overrides.clear()


def test_availability_endpoint_handles_blocking_booking_with_null_times(db, hamilton, unassigned_space):
    """Regression: a migrated 'confirmed' booking can have NULL start/end
    time. Serializing it into the availability response used to crash
    with a pydantic ValidationError (start_time/end_time were declared
    non-optional) instead of returning it as a blocking booking with
    unknown times."""
    _book(
        db,
        unassigned_space,
        start=None,
        end=None,
        status=BookingStatus.confirmed,
        event_name="Migrated booking",
    )

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(unassigned_space.id)})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404  # unassigned_space is not bookable -- see next test for why


def test_availability_endpoint_rejects_non_bookable_space(db, hamilton, unassigned_space):
    """The internal migration-triage placeholder space must never be
    queryable as if it were a real bookable space."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(unassigned_space.id)})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_is_space_free_serializes_null_time_blocking_booking_via_real_space(db, hamilton, loft):
    """Same NULL-time crash, but through a real bookable space (a
    'tentative'/'confirmed' booking can legitimately have no time set
    yet), so the 200 response path itself is exercised end-to-end."""
    _book(db, loft, start=None, end=None, status=BookingStatus.confirmed, event_name="No time yet")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(loft.id)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_free"] is False
        assert body["blocking_bookings"][0]["start_time"] is None
        assert body["blocking_bookings"][0]["end_time"] is None
    finally:
        app.dependency_overrides.clear()
