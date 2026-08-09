import datetime as dt

from app.services.validation import validate_booking_time


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
