import datetime as dt

from app.services.validation import validate_booking_time, validate_setup_access_time, validate_trading_hours


def codes(warnings):
    return {w.code for w in warnings}


def test_saturday_daytime_running_late_warns():
    # Saturday, starts 11am, ends 6pm -> breaches the 5pm daytime finish
    warnings = validate_booking_time(dt.date(2026, 8, 8), dt.time(11, 0), dt.time(18, 0))
    assert "saturday_daytime_finish" in codes(warnings)


def test_saturday_daytime_finishing_on_time_is_clean():
    warnings = validate_booking_time(dt.date(2026, 8, 8), dt.time(11, 0), dt.time(16, 30))
    assert codes(warnings) == set()


def test_saturday_evening_function_not_subject_to_daytime_rule():
    # Starts at 6pm (after the daytime cutoff) and runs to 10pm -- an
    # evening function, so the daytime-finish rule doesn't apply.
    warnings = validate_booking_time(dt.date(2026, 8, 8), dt.time(18, 0), dt.time(22, 0))
    assert "saturday_daytime_finish" not in codes(warnings)


def test_weekday_daytime_finish_not_enforced():
    # Same late finish, but it's a Wednesday -- rule is Saturday-only.
    warnings = validate_booking_time(dt.date(2026, 8, 5), dt.time(11, 0), dt.time(18, 0))
    assert "saturday_daytime_finish" not in codes(warnings)


def test_music_after_1130pm_warns_any_day():
    warnings = validate_booking_time(dt.date(2026, 8, 5), dt.time(19, 0), dt.time(23, 45))
    assert "music_off_time" in codes(warnings)


def test_music_before_1130pm_is_clean():
    warnings = validate_booking_time(dt.date(2026, 8, 5), dt.time(19, 0), dt.time(23, 0))
    assert "music_off_time" not in codes(warnings)


def test_setup_access_before_standard_requires_confirmation():
    warnings = validate_setup_access_time(dt.time(12, 0))
    assert "setup_access_requires_confirmation" in codes(warnings)


def test_setup_access_at_standard_is_clean():
    warnings = validate_setup_access_time(dt.time(14, 0))
    assert codes(warnings) == set()


def test_setup_access_after_standard_is_clean():
    warnings = validate_setup_access_time(dt.time(15, 30))
    assert codes(warnings) == set()


# RULE REPLACED, 2026-09-14. These four tests used to assert that a
# Wednesday or Thursday function finishing after 9:00pm must be flagged.
# 9:00pm is the published RESTAURANT closing time; the venue is licensed to
# midnight EVERY night and midweek functions regularly run past 9pm (Aaron).
# The rule now checks the licence, which does not vary by day.


def test_a_midweek_function_past_9pm_is_ordinary(db=None):
    """The case the old rule flagged on every midweek enquiry. Thursday
    2026-08-06 finishing at 9:30pm is a normal function."""
    warnings = validate_trading_hours(dt.date(2026, 8, 6), dt.time(21, 30), dt.time(18, 0))
    assert codes(warnings) == set()


def test_a_wednesday_function_past_9pm_is_ordinary():
    warnings = validate_trading_hours(dt.date(2026, 8, 5), dt.time(21, 30), dt.time(18, 0))
    assert codes(warnings) == set()


def test_a_finish_past_midnight_warns_on_any_night():
    """What actually breaches the licence. An end time BEFORE the start is
    how a past-midnight finish shows up -- the column holds a time of day
    with no date, so 12:30am and 12:30pm are told apart by which side of
    the start they fall on."""
    for day in (dt.date(2026, 8, 5), dt.date(2026, 8, 6), dt.date(2026, 8, 7), dt.date(2026, 8, 8)):
        warnings = validate_trading_hours(day, dt.time(0, 30), dt.time(19, 0))
        assert "finish_after_licensed_close" in codes(warnings), day.strftime("%A")


def test_it_says_nothing_without_a_start_time():
    """With no start to compare against there is nothing this can conclude,
    and a warning it cannot justify is worse than silence."""
    warnings = validate_trading_hours(dt.date(2026, 8, 6), dt.time(0, 30))
    assert codes(warnings) == set()


def test_a_friday_late_finish_is_still_ordinary():
    warnings = validate_trading_hours(dt.date(2026, 8, 7), dt.time(23, 0), dt.time(18, 0))
    assert codes(warnings) == set()
