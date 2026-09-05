"""Regression tests for the booking calendar. Per the build brief: don't
test that the calendar renders, test that it makes the specific mistakes
it exists to prevent impossible or visible. Each test below is drawn from
a named real scenario, not a hypothetical.
"""

import datetime as dt
from decimal import Decimal
import random
import re
import threading
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Space, Venue
from app.models.booking import BLOCKING_STATUSES, Booking, BookingStatus
from app.services import calendar as calendar_service
from app.services.availability import is_space_free
from app.services.booking import change_status, create_booking, create_hold, set_hold_expiry
from tests.conftest import TestSessionLocal


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


# --- CALENDAR_STATUSES must never drift from BLOCKING_STATUSES --------------


def test_calendar_statuses_is_built_from_blocking_statuses_not_a_separate_list():
    # BLOCKING_STATUSES (app.models.booking) is what the exclusion
    # constraint and is_space_free() actually enforce. If CALENDAR_STATUSES
    # were ever hand-edited back into an independent tuple, this is the
    # test that would catch it drifting out of sync.
    assert set(BLOCKING_STATUSES) <= set(calendar_service.CALENDAR_STATUSES)
    for status in BLOCKING_STATUSES:
        assert status in calendar_service.CALENDAR_STATUSES


def test_calendar_entry_is_blocking_matches_blocking_statuses(db, hamilton, loft):
    date = dt.date(2027, 4, 10)  # a Saturday
    blocking = _make_booking(
        db, loft, event_date=date, event_name="Confirmed Blocking", status=BookingStatus.confirmed,
        start_time=dt.time(12, 0), end_time=dt.time(17, 0),
    )
    non_blocking = _make_booking(db, loft, event_date=date, event_name="Enquiry Not Blocking", status=BookingStatus.enquiry)

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries_by_id = {e["booking"].id: e for e in grid["cells"][loft.id][date]}
    assert entries_by_id[blocking.id]["is_blocking"] is True
    assert entries_by_id[non_blocking.id]["is_blocking"] is False


def _make_booking(db, space, *, status=BookingStatus.enquiry, event_date, event_name="Cal Test", adult_count=40, **overrides):
    kwargs = dict(
        space_id=space.id,
        contact_id=None,
        event_date=event_date,
        start_time=None,
        end_time=None,
        event_name=event_name,
        event_type=None,
        adult_count=adult_count,
        child_count=0,
        notes=None,
        actor="test",
        status=status,
    )
    kwargs.update(overrides)
    return create_booking(db, **kwargs)


def _next_saturday(start: dt.date) -> dt.date:
    days_ahead = (5 - start.weekday()) % 7
    return start + dt.timedelta(days=days_ahead or 7)


# --- the double sale (Friday 11 September) -----------------------------------


def test_double_sale_both_enquiries_visible_not_shown_free(db, hamilton, loft):
    date = dt.date(2027, 9, 10)  # a Friday
    a = _make_booking(db, loft, event_date=date, event_name="Enquiry A")
    b = _make_booking(db, loft, event_date=date, event_name="Enquiry B")

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    ids = {e["booking"].id for e in entries}
    assert a.id in ids and b.id in ids
    assert all(e["kind"] == "enquiry" for e in entries)


def test_double_sale_confirming_one_leaves_the_other_visible_as_competing(db, hamilton, loft):
    date = dt.date(2027, 9, 10)
    a = _make_booking(db, loft, event_date=date, event_name="Enquiry A", start_time=dt.time(18, 0), end_time=dt.time(23, 0))
    b = _make_booking(db, loft, event_date=date, event_name="Enquiry B")

    change_status(db, a, BookingStatus.offered, actor="test")
    change_status(db, a, BookingStatus.tentative, actor="test")
    change_status(db, a, BookingStatus.confirmed, actor="test")

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    kinds_by_id = {e["booking"].id: e["kind"] for e in entries}
    assert kinds_by_id[a.id] == "confirmed"
    assert kinds_by_id[b.id] == "enquiry"  # still visible, not silently hidden


# --- three deep (Saturday 28 November / 17 October) -------------------------


def test_three_deep_all_visible_and_countable(db, hamilton, loft):
    date = dt.date(2027, 11, 27)  # a Saturday
    names = {"First", "Second", "Third"}
    for name in names:
        _make_booking(db, loft, event_date=date, event_name=name)

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    assert len(entries) == 3
    assert {e["booking"].event_name for e in entries} == names


# --- confirmed vs hold vs enquiry must never be mistaken for one another ----


def test_confirmed_and_hold_render_with_distinct_kinds(db, hamilton, mezzanine):
    # Different dates: an untimed hold defaults to a full-day span (see
    # HOLD_FULL_DAY_START/END), which would legitimately conflict with a
    # same-room, same-day timed booking under the exclusion constraint --
    # that conflict is correct behaviour, not what this test is about.
    confirmed_date = dt.date(2027, 5, 1)
    hold_date = dt.date(2027, 5, 2)
    confirmed = _make_booking(
        db, mezzanine, event_date=confirmed_date, event_name="Confirmed One", status=BookingStatus.confirmed,
        start_time=dt.time(12, 0), end_time=dt.time(17, 0),
    )
    hold = create_hold(db, space_id=mezzanine.id, event_date=hold_date, event_name="Hold One", actor="test")

    confirmed_grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(confirmed_date))
    hold_grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(hold_date))
    confirmed_kind = confirmed_grid["cells"][mezzanine.id][confirmed_date][0]["kind"]
    hold_kind = hold_grid["cells"][mezzanine.id][hold_date][0]["kind"]
    assert confirmed_kind == "confirmed"
    assert hold_kind == "hold_active"
    assert confirmed_kind != hold_kind


def test_room_with_only_a_hold_does_not_render_as_confirmed(admin_client, db, hamilton, loft):
    date = dt.date(2027, 5, 8)
    create_hold(db, space_id=loft.id, event_date=date, event_name="Just A Hold", actor="test")

    resp = admin_client.get(f"/admin/calendar?week={calendar_service.week_start_for(date).isoformat()}")
    assert resp.status_code == 200
    assert "Just A Hold" in resp.text
    # The hold chip's own status text must say Hold, not Confirmed -- a
    # tentative-only room must never read as booked, or you decline work
    # you could have taken.
    chip_section = resp.text.split("Just A Hold")[1][:300]
    assert "Confirmed" not in chip_section


# --- the open-ended hold (Lycopodium, live now) ------------------------------


def test_open_ended_hold_renders_on_both_spaces_and_is_distinguishable_from_confirmed(
    admin_client, db, hamilton, loft, mezzanine
):
    date = dt.date(2026, 10, 8)  # a Thursday
    hold_loft = create_hold(db, space_id=loft.id, event_date=date, event_name="Lycopodium hold", actor="test")
    hold_mezz = create_hold(db, space_id=mezzanine.id, event_date=date, event_name="Lycopodium hold", actor="test")

    assert hold_loft.hold_expires_at is None
    assert hold_mezz.hold_expires_at is None
    assert hold_loft.status == BookingStatus.tentative
    assert hold_mezz.status == BookingStatus.tentative

    resp = admin_client.get(f"/admin/calendar?week={calendar_service.week_start_for(date).isoformat()}")
    assert resp.status_code == 200
    assert resp.text.count("Lycopodium hold") == 2
    assert "no expiry" in resp.text
    assert "Confirmed" not in resp.text.split("Lycopodium hold")[1][:300]


# --- the expired hold ---------------------------------------------------------


def test_expired_hold_surfaces_as_expired_not_hidden_or_active(db, hamilton, loft):
    date = _next_saturday(dt.date.today() + dt.timedelta(days=60))
    past_expiry = dt.date.today() - dt.timedelta(days=3)
    hold = create_hold(
        db, space_id=loft.id, event_date=date, event_name="Overdue Hold",
        hold_expires_at=past_expiry, actor="test",
    )

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    assert len(entries) == 1  # not silently dropped
    assert entries[0]["kind"] == "hold_expired"
    assert entries[0]["booking"].id == hold.id
    # Still genuinely blocking -- expiry is a display fact, not a status
    # change. A room held for nobody must still show as held, not free.
    assert hold.status == BookingStatus.tentative


def test_expired_hold_still_blocks_is_space_free(db, hamilton, loft):
    date = dt.date.today() + dt.timedelta(days=90)
    create_hold(
        db, space_id=loft.id, event_date=date, event_name="Overdue Hold 2",
        hold_expires_at=dt.date.today() - dt.timedelta(days=1), actor="test",
    )
    free, _ = is_space_free(db, loft.id, date)
    assert free is False


# --- reduced minimum ----------------------------------------------------------


def test_calendar_shows_actual_guest_count_not_space_standard(db, hamilton, loft):
    date = dt.date(2027, 6, 5)
    assert loft.standard_min_adults == 60
    booking = _make_booking(db, loft, event_date=date, adult_count=40, status=BookingStatus.confirmed,
                             start_time=dt.time(18, 0), end_time=dt.time(23, 0))
    from app.models.booking import MinReductionReasonCode
    from app.services.booking import set_agreed_minimum
    set_agreed_minimum(db, booking, agreed_min_adults=40, reason=MinReductionReasonCode.aaron_discretion, actor="test")

    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    assert len(entries) == 1
    assert entries[0]["booking"].adult_count == 40


# --- concurrency: the constraint stops it, not application logic ------------


def test_concurrent_confirmations_same_space_time_only_one_succeeds():
    setup_session = TestSessionLocal()
    venue = Venue(name="Calendar Concurrency Venue", slug=f"cal-conc-{uuid.uuid4().hex[:8]}")
    space = Space(
        venue=venue, name="Test Space", capacity=100, min_food_spend=0,
        standard_min_adults=0, wheelchair_accessible=False, has_per_head_shortfall_fee=True,
    )
    setup_session.add_all([venue, space])
    setup_session.commit()
    space_id, venue_id = space.id, venue.id
    setup_session.close()

    barrier = threading.Barrier(2)
    results = {}

    def attempt(key: str):
        session = TestSessionLocal()
        try:
            booking = Booking(
                space_id=space_id, event_date=dt.date(2027, 9, 10),
                start_time=dt.time(18, 0), end_time=dt.time(23, 0),
                status=BookingStatus.confirmed, event_name=f"Concurrent {key}",
                adult_count=10, child_count=0, agreed_min_adults=0,
                agreed_min_food_spend=Decimal("0"),
                pricing_locked_at=dt.date.today(),
                reference_code=f"CALC-{key}-{uuid.uuid4().hex[:8].upper()}",
            )
            session.add(booking)
            barrier.wait(timeout=5)
            session.commit()
            results[key] = "success"
        except IntegrityError:
            session.rollback()
            results[key] = "failed"
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(key,)) for key in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["failed", "success"], results

    cleanup = TestSessionLocal()
    cleanup.query(Booking).filter(Booking.space_id == space_id).delete()
    cleanup.query(Space).filter(Space.id == space_id).delete()
    cleanup.query(Venue).filter(Venue.id == venue_id).delete()
    cleanup.commit()
    cleanup.close()


# --- the empty week ------------------------------------------------------------


def test_empty_week_renders_obviously_empty_not_error(admin_client, hamilton):
    far_future = dt.date(2029, 3, 5)  # nothing seeded here
    resp = admin_client.get(f"/admin/calendar?week={calendar_service.week_start_for(far_future).isoformat()}")
    assert resp.status_code == 200
    assert resp.text.count("&mdash;") >= 21  # 3 spaces x 7 days, all empty


# --- boundary cases ------------------------------------------------------------


def test_booking_ending_near_midnight_renders(db, hamilton, loft):
    date = dt.date(2027, 9, 11)  # a Saturday
    _make_booking(
        db, loft, event_date=date, event_name="Late Finish", status=BookingStatus.confirmed,
        start_time=dt.time(19, 0), end_time=dt.time(23, 59),
    )
    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    assert len(entries) == 1
    assert entries[0]["kind"] == "confirmed"


def test_saturday_daytime_and_evening_in_same_room_render_as_two_entries_not_a_clash(db, hamilton, loft):
    date = dt.date(2027, 9, 11)  # a Saturday
    daytime = _make_booking(
        db, loft, event_date=date, event_name="Daytime Function", status=BookingStatus.confirmed,
        start_time=dt.time(11, 0), end_time=dt.time(17, 0),
    )
    evening = _make_booking(
        db, loft, event_date=date, event_name="Evening Function", status=BookingStatus.confirmed,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
    )
    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    ids = {e["booking"].id for e in entries}
    assert daytime.id in ids and evening.id in ids
    assert len(entries) == 2


def test_far_future_date_2027_renders(db, hamilton, loft):
    date = dt.date(2027, 3, 15)
    _make_booking(db, loft, event_date=date, event_name="Far Future Booking")
    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    entries = grid["cells"][loft.id][date]
    assert len(entries) == 1


def test_booking_with_no_event_date_does_not_crash_the_view_and_is_counted(admin_client, db, hamilton, unassigned_space):
    _make_booking(db, unassigned_space, event_date=None, event_name="No Date Yet")

    resp = admin_client.get("/admin/calendar")
    assert resp.status_code == 200
    assert "1 booking" in resp.text
    assert "no event date set" in resp.text


# --- the hold-creation and hold-expiry admin routes --------------------------


def test_new_hold_form_renders(admin_client, hamilton):
    resp = admin_client.get("/admin/calendar/holds/new")
    assert resp.status_code == 200
    assert "The Loft" in resp.text
    assert "The Mezzanine" in resp.text


def test_create_hold_via_dashboard_across_two_spaces(admin_client, db, hamilton, loft, mezzanine):
    page = admin_client.get("/admin/calendar/holds/new")
    csrf_token = _csrf(page.text)
    date = dt.date(2027, 10, 7)  # a Thursday

    resp = admin_client.post(
        "/admin/calendar/holds",
        data={
            "csrf_token": csrf_token,
            "space_ids": [str(loft.id), str(mezzanine.id)],
            "event_date": date.isoformat(),
            "event_name": "Lycopodium hold",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    created = db.query(Booking).filter_by(event_name="Lycopodium hold").all()
    assert len(created) == 2
    assert {b.space_id for b in created} == {loft.id, mezzanine.id}
    assert all(b.status == BookingStatus.tentative for b in created)
    assert all(b.hold_expires_at is None for b in created)
    # Untimed hold must default to a concrete span, never NULL start/end --
    # otherwise it wouldn't actually sit behind the exclusion constraint.
    assert all(b.start_time is not None and b.end_time is not None for b in created)


def test_create_hold_rejects_conflicting_space(admin_client, db, hamilton, loft):
    date = dt.date(2027, 10, 8)
    create_hold(db, space_id=loft.id, event_date=date, event_name="Existing Hold", actor="test")

    page = admin_client.get("/admin/calendar/holds/new")
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        "/admin/calendar/holds",
        data={
            "csrf_token": csrf_token, "space_ids": [str(loft.id)],
            "event_date": date.isoformat(), "event_name": "Conflicting Hold",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_set_hold_expiry_via_dashboard(admin_client, db, hamilton, loft):
    date = dt.date(2027, 10, 9)
    hold = create_hold(db, space_id=loft.id, event_date=date, event_name="Expiry Test Hold", actor="test")

    page = admin_client.get(f"/admin/bookings/{hold.id}")
    csrf_token = _csrf(page.text)
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    resp = admin_client.post(
        f"/admin/bookings/{hold.id}/hold-expiry",
        data={"csrf_token": csrf_token, "hold_expires_at": expiry},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(hold)
    assert hold.hold_expires_at.isoformat() == expiry


# --- property: calendar and GET /availability must never disagree -----------


def test_calendar_agrees_with_availability_endpoint_across_random_scenarios(admin_client, db, hamilton, loft, mezzanine, lounge):
    rng = random.Random(20260811)
    spaces = [loft, mezzanine, lounge]
    base = dt.date(2028, 1, 3)  # a Monday, far enough out to avoid other tests' dates
    dates = [base + dt.timedelta(days=i) for i in range(21)]
    statuses = [
        BookingStatus.enquiry, BookingStatus.offered, BookingStatus.tentative,
        BookingStatus.confirmed, BookingStatus.completed, BookingStatus.cancelled,
    ]

    # Scatter random bookings/holds so several dates have overlapping
    # blocking and non-blocking statuses in the same space. Two random
    # draws can legitimately collide (two blocking-status bookings, same
    # space/time) -- the exclusion constraint correctly rejects that, so
    # skip and move on rather than treating it as a test failure; that
    # constraint behaviour is already covered by
    # test_concurrent_confirmations_same_space_time_only_one_succeeds and
    # tests/test_double_booking.py.
    created = 0
    attempts = 0
    while created < 40 and attempts < 200:
        attempts += 1
        space = rng.choice(spaces)
        date = rng.choice(dates)
        status = rng.choice(statuses)
        hour = rng.choice([10, 12, 14, 18])
        try:
            _make_booking(
                db, space, event_date=date, event_name=f"Random {uuid.uuid4().hex[:6]}",
                status=status, start_time=dt.time(hour, 0), end_time=dt.time(hour + 3, 0),
            )
            created += 1
        except IntegrityError:
            db.rollback()

    for week_start in {calendar_service.week_start_for(d) for d in dates}:
        grid = calendar_service.get_week_grid(db, hamilton, week_start)
        for space in spaces:
            for day in grid["days"]:
                entries = grid["cells"][space.id][day]
                calendar_says_occupied = any(e["is_blocking"] for e in entries)
                free, _ = is_space_free(db, space.id, day)
                availability_says_occupied = not free
                assert calendar_says_occupied == availability_says_occupied, (
                    f"disagreement at space={space.name} date={day}: "
                    f"calendar={calendar_says_occupied} availability={availability_says_occupied}"
                )
