"""Booking-time policy checks.

These are business rules Aaron stated directly (no Handover doc was
available at build time to confirm the exact wording/scope) — treat the
thresholds as provisional and verify against the real Handover docs.
Both rules are enforced as warnings, not rejections: the caller decides
what to do with them, nothing here blocks a booking from being made.
"""

import datetime as dt
from dataclasses import dataclass

SATURDAY = 5  # datetime.date.weekday(): Monday=0 ... Sunday=6
DAYTIME_CUTOFF = dt.time(17, 0)
MUSIC_OFF_TIME = dt.time(23, 30)


@dataclass
class ValidationWarning:
    code: str
    message: str


def validate_booking_time(event_date: dt.date, start_time: dt.time, end_time: dt.time) -> list[ValidationWarning]:
    warnings: list[ValidationWarning] = []

    # "Daytime" is inferred as starting before the 5pm cutoff itself, since
    # no Handover doc defines the boundary between a daytime and an evening
    # function. An evening Saturday function is not subject to this rule.
    is_saturday_daytime = event_date.weekday() == SATURDAY and start_time < DAYTIME_CUTOFF
    if is_saturday_daytime and end_time > DAYTIME_CUTOFF:
        warnings.append(
            ValidationWarning(
                code="saturday_daytime_finish",
                message=f"Saturday daytime functions must finish by 5:00pm — this booking ends at {end_time.strftime('%I:%M%p').lstrip('0').lower()}.",
            )
        )

    if end_time > MUSIC_OFF_TIME:
        warnings.append(
            ValidationWarning(
                code="music_off_time",
                message=f"Music must be off by 11:30pm any day — this booking ends at {end_time.strftime('%I:%M%p').lstrip('0').lower()}.",
            )
        )

    return warnings
