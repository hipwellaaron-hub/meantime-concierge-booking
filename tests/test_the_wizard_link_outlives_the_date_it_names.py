"""A wizard link does not expire before the date its own email names.

The resume email tells the client an ABSOLUTE due date -- event_date minus
the wizard lead time -- and the link expired a flat 21 days after it was
issued. Two unrelated clocks. Send a wizard early, which is what staff do
when a client asks months ahead, and the link is dead well before the date
the client was given, with the email still naming it. The client clicks,
gets "this link is no longer available", and there is no route that
re-issues one.

policy.wizard_token_expiry takes the LATER of the two: the flat 21 days,
or a few days past the promised date. An on-time wizard is unaffected --
when the link goes out at the lead time, the due date is roughly today and
21 days is already the later figure.

EVERY PROBE ISSUES THE LINK EARLY. On the normal schedule the two clocks
agree and a flat TTL would pass.
"""
import datetime as dt

from app.models.booking import BookingStatus
from app.services import policy, wizard as wizard_service
from app.services.booking import change_status, create_booking

NOW = dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.timezone.utc)


def _booking(db, loft, contact, name, *, event_in_days):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=event_in_days), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()
    change_status(db, b, BookingStatus.confirmed, actor="test")
    return b


def _promised_due(booking):
    return booking.event_date - dt.timedelta(days=policy.WIZARD_TRIGGER_DAYS_BEFORE_EVENT)


# --- the helper ------------------------------------------------------------------


def test_an_early_link_lives_past_the_date_the_client_is_told():
    """THE one. Event six months out, wizard sent today: the promised due
    date is months away and a flat 21 days dies long before it."""
    event = NOW.date() + dt.timedelta(days=180)

    expiry = policy.wizard_token_expiry(event, created_at=NOW)

    due = event - dt.timedelta(days=policy.WIZARD_TRIGGER_DAYS_BEFORE_EVENT)
    assert expiry.date() > due, "the link dies before the date its own email names"
    assert expiry.date() == due + dt.timedelta(days=policy.WIZARD_TOKEN_GRACE_DAYS_AFTER_DUE)


def test_an_on_time_link_keeps_the_flat_twenty_one_days():
    """Sent at the lead time, the due date is roughly today, so Aaron's 21
    days is already the later figure and nothing changes."""
    event = NOW.date() + dt.timedelta(days=policy.WIZARD_TRIGGER_DAYS_BEFORE_EVENT)

    expiry = policy.wizard_token_expiry(event, created_at=NOW)

    assert expiry == NOW + dt.timedelta(days=policy.WIZARD_TOKEN_TTL_DAYS)


def test_a_late_link_keeps_the_flat_twenty_one_days():
    """Sent after the due date has passed -- the client still gets their
    full window."""
    event = NOW.date() + dt.timedelta(days=2)

    expiry = policy.wizard_token_expiry(event, created_at=NOW)

    assert expiry == NOW + dt.timedelta(days=policy.WIZARD_TOKEN_TTL_DAYS)


def test_an_undated_booking_falls_back_to_the_flat_ttl():
    assert policy.wizard_token_expiry(None, created_at=NOW) == NOW + dt.timedelta(
        days=policy.WIZARD_TOKEN_TTL_DAYS
    )


# --- and the session it issues ------------------------------------------------------


def test_a_session_for_a_distant_event_outlives_its_promised_date(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZWIZ Distant", event_in_days=180)

    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")

    assert session.expires_at.date() > _promised_due(booking), (
        "the issued link expires before the date the resume email names"
    )


def test_a_session_for_a_near_event_is_unchanged(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZWIZ Near", event_in_days=10)

    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")

    expected = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=policy.WIZARD_TOKEN_TTL_DAYS)
    assert abs((session.expires_at - expected).total_seconds()) < 120


def test_the_link_is_still_usable_on_the_promised_date(db, hamilton, loft, contact):
    """The point of the whole change, asked of is_usable rather than of the
    column: a client opening it on the day they were told must get in."""
    booking = _booking(db, loft, contact, "ZZWIZ OnTheDay", event_in_days=180)
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")

    on_the_day = dt.datetime.combine(
        _promised_due(booking), dt.time(12, 0), tzinfo=dt.timezone.utc
    )

    assert wizard_service.is_usable(session, now=on_the_day) is True


def test_it_still_expires_eventually(db, hamilton, loft, contact):
    """A link that never dies is not an expiry. Well past the grace, it is
    refused."""
    booking = _booking(db, loft, contact, "ZZWIZ Eventually", event_in_days=180)
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")

    long_after = dt.datetime.combine(
        booking.event_date + dt.timedelta(days=30), dt.time(12, 0), tzinfo=dt.timezone.utc
    )

    assert wizard_service.is_usable(session, now=long_after) is False
