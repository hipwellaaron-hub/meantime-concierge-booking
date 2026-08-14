import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, BookingEvent
from app.services.booking import create_booking
from app.services.enquiry_classification import (
    BIRTHDAY_CLARIFICATION_QUESTION,
    classify_and_flag,
    get_enquiries_needing_clarification,
    get_flagged_bookings_in_progress,
)


def _next_weekday(start: dt.date, target_weekday: int) -> dt.date:
    days_ahead = (target_weekday - start.weekday()) % 7
    days_ahead = days_ahead or 7
    return start + dt.timedelta(days=days_ahead)


def _next_saturday(start: dt.date) -> dt.date:
    return _next_weekday(start, 5)


def _next_friday(start: dt.date) -> dt.date:
    return _next_weekday(start, 4)


def _next_wednesday(start: dt.date) -> dt.date:
    return _next_weekday(start, 2)


def _next_thursday(start: dt.date) -> dt.date:
    return _next_weekday(start, 3)


def _make_booking(db, space, *, event_date, event_name="Test Enquiry", notes=None):
    return create_booking(
        db,
        space_id=space.id,
        contact_id=None,
        event_date=event_date,
        event_name=event_name,
        event_type=None,
        adult_count=50,
        child_count=0,
        notes=notes,
        actor="test",
    )


# --- generic "Birthday" ambiguity -----------------------------------------


def test_generic_birthday_is_flagged_for_clarification(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
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


def test_18th_birthday_dropdown_does_not_trigger_generic_birthday_flag(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("milestone and guest ages not yet known" in f for f in flags)


def test_21st_birthday_does_not_trigger_generic_birthday_flag(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="21st Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("milestone and guest ages not yet known" in f for f in flags)


# --- free-text / event-name "18" mining -------------------------------------


def test_18_in_event_name_is_detected_even_with_generic_dropdown_type(db, unassigned_space):
    """The milestone is frequently only disclosed in the event name, never
    the dropdown -- a real example: event name "18 birthday", dropdown
    still says "Birthday"."""
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)), event_name="18 birthday")
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
    assert any("18th birthday confirmed" in f for f in flags)
    assert not any("milestone and guest ages not yet known" in f for f in flags)


def test_18th_in_comments_is_detected_even_with_generic_dropdown_type(db, unassigned_space):
    """Another real example: the event name gives no hint at all, but the
    free-text comment says "18th birthday celebration for both"."""
    booking = _make_booking(
        db,
        unassigned_space,
        event_date=_next_friday(dt.date(2027, 1, 1)),
        event_name="Aysha and Byron's Birthday",
        notes="18th birthday celebration for both",
    )
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
    assert any("18th birthday confirmed" in f for f in flags)
    assert not any("milestone and guest ages not yet known" in f for f in flags)


def test_21st_mentioning_18_year_old_guests_is_not_detected_as_18th(db, unassigned_space):
    """A 21st that happens to mention guests under 18 must not be
    misread as an 18th -- the pattern requires "18" to be immediately
    followed by "birthday"/"bday", not just present anywhere."""
    booking = _make_booking(
        db,
        unassigned_space,
        event_date=_next_friday(dt.date(2027, 1, 1)),
        event_name="Harrison's 21st",
        notes="A few of the guests are 18 years old",
    )
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("18th birthday confirmed" in f for f in flags)
    assert any("milestone and guest ages not yet known" in f for f in flags)


# --- missing adult/minor split -----------------------------------------------


def test_missing_adult_count_is_flagged_regardless_of_event_type(db, unassigned_space):
    """Minimums count adults only, no matter what kind of event this is --
    the split rule applies universally, not just to birthdays."""
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(
        db, booking, event_type="Baby Shower", adult_count=None, attendee_count=40, actor="test"
    )
    assert any("Adult/minor guest split not provided" in f for f in flags)


def test_adult_count_given_is_not_flagged_for_split(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(
        db, booking, event_type="21st Birthday", adult_count=45, attendee_count=45, actor="test"
    )
    assert not any("Adult/minor guest split not provided" in f for f in flags)


def test_missing_adult_count_not_flagged_when_attendee_count_also_missing(db, unassigned_space):
    """Nothing to compare the split against without a total guest count --
    the missing-count flag covers this case instead."""
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(
        db, booking, event_type="Wedding", adult_count=None, attendee_count=None, actor="test"
    )
    assert not any("Adult/minor guest split not provided" in f for f in flags)
    assert any("Guest count not provided" in f for f in flags)


# --- 18th birthday + Saturday -----------------------------------------------


def test_18th_birthday_on_saturday_is_flagged(db, unassigned_space):
    saturday = _next_saturday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=saturday)
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, attendee_count=40, actor="test")
    assert any("Saturday" in f and "18th" in f for f in flags)


def test_18th_birthday_on_friday_is_not_flagged_for_saturday(db, unassigned_space):
    friday = _next_friday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=friday)
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("Saturday" in f for f in flags)


def test_21st_birthday_on_saturday_is_not_flagged(db, unassigned_space):
    """The Saturday restriction is specific to 18ths -- a 21st on a
    Saturday is normal and must not be flagged."""
    saturday = _next_saturday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=saturday)
    flags = classify_and_flag(db, booking, event_type="21st Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("Saturday" in f for f in flags)


def test_18th_detected_via_text_on_saturday_is_flagged(db, unassigned_space):
    """The Saturday check must apply to a text-detected 18th too, not
    just an exact dropdown match -- this is the flagship case: a real
    18th, disclosed only in the event name, landing on a Saturday."""
    saturday = _next_saturday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=saturday, event_name="18 birthday")
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=None, actor="test")
    assert any("Saturday" in f and "18th" in f for f in flags)


# --- generic / unclear event type --------------------------------------------


def test_generic_event_types_are_flagged(db, unassigned_space):
    for generic_type in ("Party", "Event", "Other", "Not sure yet"):
        booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
        flags = classify_and_flag(
            db, booking, event_type=generic_type, adult_count=40, attendee_count=40, actor="test"
        )
        assert any("is unclear" in f for f in flags), f"{generic_type} should have been flagged"


def test_specific_event_types_are_not_flagged_as_generic(db, unassigned_space):
    for real_type in ("Wedding", "Corporate Function", "Christmas Party", "Christmas"):
        booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
        flags = classify_and_flag(
            db, booking, event_type=real_type, adult_count=40, attendee_count=40, actor="test"
        )
        assert not any("is unclear" in f for f in flags), f"{real_type} should not have been flagged"


# --- missing attendee count / event date -------------------------------------


def test_missing_attendee_count_is_flagged(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Wedding", adult_count=None, attendee_count=None, actor="test")
    assert any("Guest count not provided" in f for f in flags)


def test_missing_event_date_is_flagged(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=None)
    flags = classify_and_flag(db, booking, event_type="Wedding", adult_count=80, attendee_count=80, actor="test")
    assert any("Event date not provided" in f for f in flags)


def test_missing_event_date_does_not_crash_the_saturday_or_thursday_checks(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=None)
    flags = classify_and_flag(db, booking, event_type="18th Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("Saturday" in f for f in flags)
    assert not any("Thursday" in f for f in flags)


# --- midweek (Wednesday/Thursday) trading hours ---------------------------------


def test_thursday_is_flagged(db, unassigned_space):
    thursday = _next_thursday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=thursday)
    flags = classify_and_flag(db, booking, event_type="Corporate Function", adult_count=50, attendee_count=50, actor="test")
    assert any("Thursday" in f for f in flags)


def test_wednesday_is_flagged(db, unassigned_space):
    """Master Policy §1.8's trading-hours clause covers Wednesday and
    Thursday identically -- this was previously checked only for Thursday."""
    wednesday = _next_wednesday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=wednesday)
    flags = classify_and_flag(db, booking, event_type="Corporate Function", adult_count=50, attendee_count=50, actor="test")
    assert any("Wednesday" in f for f in flags)


def test_friday_is_not_flagged_for_midweek_trading(db, unassigned_space):
    friday = _next_friday(dt.date(2027, 1, 1))
    booking = _make_booking(db, unassigned_space, event_date=friday)
    flags = classify_and_flag(db, booking, event_type="Corporate Function", adult_count=50, attendee_count=50, actor="test")
    assert not any("Thursday" in f for f in flags)
    assert not any("Wednesday" in f for f in flags)


# --- accessibility -------------------------------------------------------------


def test_accessibility_mention_is_flagged(db, unassigned_space, lounge):
    booking = _make_booking(
        db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)),
        notes="Do you have a lift if room I not ground floor?",
    )
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
    assert any("Accessibility need raised" in f for f in flags)


def test_accessibility_mention_with_guest_count_over_accessible_capacity_is_flagged(db, unassigned_space, lounge):
    # The Lounge (the only wheelchair-accessible space) caps at 35.
    booking = _make_booking(
        db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)),
        notes="Wondering about wheelchair access to the function spaces",
    )
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=70, attendee_count=70, actor="test")
    assert any("exceeds the only wheelchair-accessible space" in f for f in flags)


def test_accessibility_mention_with_guest_count_within_accessible_capacity_is_not_flagged_for_capacity(
    db, unassigned_space, lounge
):
    booking = _make_booking(
        db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)),
        notes="Do we need to worry about stairs?",
    )
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=20, attendee_count=20, actor="test")
    assert any("Accessibility need raised" in f for f in flags)
    assert not any("exceeds the only wheelchair-accessible space" in f for f in flags)


def test_no_accessibility_mention_is_not_flagged(db, unassigned_space, lounge):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Birthday", adult_count=40, attendee_count=40, actor="test")
    assert not any("Accessibility" in f for f in flags)


# --- exceeds every space's capacity ------------------------------------------


def test_guest_count_exceeding_every_space_is_flagged(db, unassigned_space, loft, mezzanine, lounge):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(
        db, booking, event_type="Engagement Party", adult_count=150, attendee_count=150, actor="test"
    )
    assert any("exceeds every single space's capacity" in f for f in flags)


def test_guest_count_within_largest_space_is_not_flagged(db, unassigned_space, loft, mezzanine, lounge):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(
        db, booking, event_type="Engagement Party", adult_count=80, attendee_count=80, actor="test"
    )
    assert not any("exceeds every single space's capacity" in f for f in flags)


# --- persistence + idempotence ----------------------------------------------


def test_flags_are_persisted_as_booking_events(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
    # Generic birthday + missing adult split = two distinct flags
    assert len(events) == 2


def test_clean_enquiry_raises_no_flags(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    flags = classify_and_flag(db, booking, event_type="Wedding", adult_count=80, attendee_count=80, actor="test")
    assert flags == []
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
    assert events == []


# --- staff worklist ----------------------------------------------------------


def test_get_enquiries_needing_clarification_lists_flagged_open_enquiries(db, hamilton, unassigned_space):
    flagged = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)), event_name="Flagged One")
    classify_and_flag(db, flagged, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    clean = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 8)), event_name="Clean One")
    classify_and_flag(db, clean, event_type="Wedding", adult_count=80, attendee_count=80, actor="test")

    results = get_enquiries_needing_clarification(db, hamilton)
    result_ids = {b.id for b in results}
    assert flagged.id in result_ids
    assert clean.id not in result_ids


def test_flagged_enquiry_drops_off_worklist_once_progressed(db, hamilton, unassigned_space):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    assert booking.id in {b.id for b in get_enquiries_needing_clarification(db, hamilton)}

    change_status(db, booking, BookingStatus.offered, actor="test")
    assert booking.id not in {b.id for b in get_enquiries_needing_clarification(db, hamilton)}


def test_flagged_booking_moves_from_enquiry_worklist_to_in_progress_worklist(db, hamilton, unassigned_space):
    """A flag is never silently dropped just because the booking
    progressed -- it moves from the enquiry-stage worklist to the
    in-progress one instead of disappearing."""
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    assert booking.id not in {b.id for b in get_flagged_bookings_in_progress(db, hamilton)}

    change_status(db, booking, BookingStatus.offered, actor="test")
    assert booking.id in {b.id for b in get_flagged_bookings_in_progress(db, hamilton)}
    assert booking.id not in {b.id for b in get_enquiries_needing_clarification(db, hamilton)}

    change_status(db, booking, BookingStatus.tentative, actor="test")
    assert booking.id in {b.id for b in get_flagged_bookings_in_progress(db, hamilton)}

    change_status(db, booking, BookingStatus.cancelled, actor="test")
    assert booking.id not in {b.id for b in get_flagged_bookings_in_progress(db, hamilton)}


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


# --- enquiry notification email ---------------------------------------------


from unittest.mock import patch

from app.services import enquiry_classification, notifications
from app.services.enquiry_classification import (
    get_enquiry_notification_failures,
    notify_new_enquiry,
    resend_enquiry_notification,
)


def test_notify_new_enquiry_records_success_and_sets_sent_at(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=True), \
         patch.object(notifications, "send_enquiry_notification_email") as mock_send:
        notify_new_enquiry(db, booking, flags=[], actor="test")

    mock_send.assert_called_once()
    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is not None
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_notification_sent").all()
    assert len(events) == 1


def test_notify_new_enquiry_not_configured_records_failure_without_retry(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=False), \
         patch.object(notifications, "send_enquiry_notification_email") as mock_send:
        notify_new_enquiry(db, booking, flags=[], actor="test")

    mock_send.assert_not_called()
    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is None
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_notification_failed").all()
    assert len(events) == 1
    assert "not configured" in events[0].new_value.lower()


def test_notify_new_enquiry_retries_once_then_succeeds(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=True), \
         patch.object(notifications, "send_enquiry_notification_email", side_effect=[RuntimeError("boom"), None]) as mock_send, \
         patch.object(enquiry_classification.time, "sleep") as mock_sleep:
        notify_new_enquiry(db, booking, flags=[], actor="test")

    assert mock_send.call_count == 2
    mock_sleep.assert_called_once()
    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is not None
    failure_events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_notification_failed").all()
    assert len(failure_events) == 0  # the eventual success clears any need to surface a failure


def test_notify_new_enquiry_records_failure_after_exhausting_retry(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=True), \
         patch.object(notifications, "send_enquiry_notification_email", side_effect=RuntimeError("Gmail is down")) as mock_send, \
         patch.object(enquiry_classification.time, "sleep"):
        notify_new_enquiry(db, booking, flags=[], actor="test")

    assert mock_send.call_count == 2
    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is None
    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_notification_failed").all()
    assert len(events) == 1
    assert "Gmail is down" in events[0].new_value


def test_notify_new_enquiry_never_raises_even_on_total_failure(db, unassigned_space):
    """The public /enquiries endpoint -- and the client's own thank-you
    page -- must never fail just because the venue's own notification
    email couldn't send."""
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=True), \
         patch.object(notifications, "send_enquiry_notification_email", side_effect=RuntimeError("boom")), \
         patch.object(enquiry_classification.time, "sleep"):
        notify_new_enquiry(db, booking, flags=[], actor="test")  # must not raise


def test_get_enquiry_notification_failures_excludes_bookings_never_attempted(db, hamilton, loft):
    # An iVvy-imported / manually-held booking never goes through
    # notify_new_enquiry at all -- it must never appear here just because
    # enquiry_notification_sent_at also happens to be NULL for it.
    _make_booking(db, loft, event_date=dt.date(2027, 4, 1), event_name="Never Attempted")

    result = get_enquiry_notification_failures(db, hamilton)
    assert "Never Attempted" not in {b.event_name for b in result}


def test_get_enquiry_notification_failures_lists_and_clears_on_resend(db, hamilton, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1), event_name="Failed Notification Booking")
    with patch.object(notifications, "is_gmail_smtp_configured", return_value=False):
        notify_new_enquiry(db, booking, flags=[], actor="test")

    result = get_enquiry_notification_failures(db, hamilton)
    assert booking.id in {b.id for b in result}

    with patch.object(notifications, "send_enquiry_notification_email"):
        resend_enquiry_notification(db, booking, actor="staff:test")

    result_after = get_enquiry_notification_failures(db, hamilton)
    assert booking.id not in {b.id for b in result_after}


def test_resend_enquiry_notification_uses_flags_already_recorded(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=_next_friday(dt.date(2027, 1, 1)))
    classify_and_flag(db, booking, event_type="Birthday", adult_count=None, attendee_count=40, actor="test")

    with patch.object(notifications, "send_enquiry_notification_email") as mock_send:
        resend_enquiry_notification(db, booking, actor="staff:test")

    _, kwargs = mock_send.call_args
    assert len(kwargs["flags"]) == 2


def test_resend_enquiry_notification_raises_and_records_failure(db, unassigned_space):
    booking = _make_booking(db, unassigned_space, event_date=dt.date(2027, 4, 1))

    with patch.object(notifications, "send_enquiry_notification_email", side_effect=RuntimeError("still down")):
        with pytest.raises(RuntimeError):
            resend_enquiry_notification(db, booking, actor="staff:test")

    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_notification_failed").all()
    assert len(events) == 1
    assert "still down" in events[0].new_value
