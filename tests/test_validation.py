import datetime as dt

from app.services.validation import validate_booking_time, validate_setup_access_time, validate_trading_hours


def codes(warnings):
    return {w.code for w in warnings}


def test_saturday_daytime_running_late_warns():
    # Saturday, starts 11am, ends 6pm -> breaches the 5pm daytime finish
    warnings = validate_booking_time(dt.date(2026, 8, 8), dt.time(11, 0), dt.time(18, 0))
    assert "saturday_daytime_finish" in codes(warnings)


def test_saturday_daytime_finishing_on_time_is_clean():
    warnings = validate_booking_time(dt.date(2026, 8, 8), dt.time(11, 0), dt.time(17, 0))
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


def test_thursday_finish_after_close_warns():
    # Thursday 2026-08-06, finishing at 9:30pm -- after the 9pm close.
    warnings = validate_trading_hours(dt.date(2026, 8, 6), dt.time(21, 30))
    assert "thursday_finish_after_close" in codes(warnings)


def test_thursday_finish_at_close_is_clean():
    warnings = validate_trading_hours(dt.date(2026, 8, 6), dt.time(21, 0))
    assert codes(warnings) == set()


def test_friday_finish_after_9pm_not_subject_to_thursday_rule():
    # Same late finish, but it's a Friday -- rule is Thursday-only.
    warnings = validate_trading_hours(dt.date(2026, 8, 7), dt.time(23, 0))
    assert "thursday_finish_after_close" not in codes(warnings)
