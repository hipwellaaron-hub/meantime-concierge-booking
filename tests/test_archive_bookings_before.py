import datetime as dt

from app.archive_bookings_before import archive_bookings_before
from app.models import Booking, BookingEvent
from app.models.booking import BookingStatus
from app.services.availability import is_space_free
from app.services.booking import change_status, create_booking


def _booking(db, space, *, event_date, event_name, status=BookingStatus.confirmed, **overrides):
    booking = create_booking(
        db, space_id=space.id, contact_id=None, event_date=event_date, event_name=event_name,
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test", **overrides,
    )
    if status != BookingStatus.enquiry:
        change_status(db, booking, status, actor="test")
    return booking


def test_archives_bookings_on_or_before_cutoff(db, loft):
    in_range = _booking(db, loft, event_date=dt.date(2026, 9, 30), event_name="In Range")
    out_of_range = _booking(db, loft, event_date=dt.date(2026, 10, 1), event_name="Out Of Range")

    archived = archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")

    assert {b.id for b in archived} == {in_range.id}
    db.refresh(in_range)
    db.refresh(out_of_range)
    assert in_range.status == BookingStatus.archived
    assert out_of_range.status == BookingStatus.confirmed


def test_never_touches_bookings_with_no_event_date(db, unassigned_space):
    undated = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None, event_name="Undated Enquiry",
        event_type=None, adult_count=5, child_count=0, notes=None, actor="test",
    )

    archived = archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")

    assert undated.id not in {b.id for b in archived}
    db.refresh(undated)
    assert undated.status == BookingStatus.enquiry


def test_is_idempotent(db, loft):
    _booking(db, loft, event_date=dt.date(2026, 8, 1), event_name="Once")

    first_pass = archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")
    second_pass = archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")

    assert len(first_pass) == 1
    assert second_pass == []


def test_frees_the_date_for_a_real_double_booking(db, loft):
    booking = _booking(db, loft, event_date=dt.date(2026, 7, 24), event_name="Pre-Launch Wedding")
    booking.start_time, booking.end_time = dt.time(18, 0), dt.time(23, 0)
    db.commit()

    was_free_before, _ = is_space_free(db, loft.id, dt.date(2026, 7, 24))
    assert was_free_before is False

    archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")

    is_free_now, blocking = is_space_free(db, loft.id, dt.date(2026, 7, 24))
    assert is_free_now is True
    assert blocking == []


def test_audit_trail_records_the_previous_status_and_a_reason(db, loft):
    booking = _booking(db, loft, event_date=dt.date(2026, 7, 24), event_name="Real 21st", status=BookingStatus.confirmed)

    archive_bookings_before(db, dt.date(2026, 9, 30), actor="maintenance:test")

    events = (
        db.query(BookingEvent)
        .filter_by(booking_id=booking.id, event_type="status_changed")
        .order_by(BookingEvent.created_at)
        .all()
    )
    # The booking already went enquiry -> confirmed when _booking() set it
    # up -- the archive transition is the *last* status-changed pair.
    status_event = [e for e in events if e.field_name == "status"][-1]
    reason_event = [e for e in events if e.field_name == "status_change_reason"][-1]
    assert status_event.old_value == "confirmed"
    assert status_event.new_value == "archived"
    assert status_event.actor == "maintenance:test"
    assert "Archived ahead of go-live" in reason_event.new_value


def test_archived_is_distinct_from_cancelled(db, loft):
    """The whole reason this is a new status rather than reusing
    `cancelled`: a real cancellation and pre-launch cleanup must never
    look the same on the booking's own page."""
    archived = _booking(db, loft, event_date=dt.date(2026, 7, 1), event_name="Archived One")
    cancelled = _booking(db, loft, event_date=dt.date(2026, 10, 5), event_name="Really Cancelled")
    change_status(db, cancelled, BookingStatus.cancelled, actor="test")

    archive_bookings_before(db, dt.date(2026, 9, 30), actor="test")

    db.refresh(archived)
    db.refresh(cancelled)
    assert archived.status == BookingStatus.archived
    assert cancelled.status == BookingStatus.cancelled
    assert archived.status != cancelled.status


def test_archived_has_no_legal_transitions_out():
    from app.services.booking import LEGAL_TRANSITIONS

    assert LEGAL_TRANSITIONS[BookingStatus.archived] == ()


def test_archived_is_never_offered_as_a_manual_status_change_target():
    """Archiving is a one-off maintenance operation, not a normal staff
    workflow action -- no status's legal-transition list should ever
    offer it."""
    from app.services.booking import LEGAL_TRANSITIONS

    for targets in LEGAL_TRANSITIONS.values():
        assert BookingStatus.archived not in targets
