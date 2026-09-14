"""Booking-time policy checks.

The Saturday-daytime and music-off rules below were originally provisional
(no Handover doc existed at build time) and are now confirmed verbatim
against the Meantime Hamilton Master Policy v1.3 doc -- both figures
matched exactly, no changes needed. The two additions below (setup access,
Thursday trading) are sourced from that same doc. Every rule here is
enforced as a warning, not a rejection: the caller decides what to do with
them, nothing here blocks a booking from being made.
"""

import datetime as dt
from dataclasses import dataclass


def _clock(value: dt.time) -> str:
    return value.strftime('%I:%M%p').lstrip('0').lower()

SATURDAY = 5  # datetime.date.weekday(): Monday=0 ... Sunday=6
WEDNESDAY = 2
THURSDAY = 3
# Saturday daytime functions finish at 4:30pm (Aaron, 2026-09-03; the code
# said 5:00pm in three places before this). Messages format this constant
# rather than repeating the time as text.
DAYTIME_CUTOFF = dt.time(16, 30)
MUSIC_OFF_TIME = dt.time(23, 30)
# Master Policy v1.3 §1.8: "From 2:00pm standard. Earlier is often
# possible but must be confirmed, never promised."
SETUP_ACCESS_STANDARD_TIME = dt.time(14, 0)
# The FUNCTION licence, which is the constraint a booking actually has:
# licensed to midnight, every night (Aaron, 2026-09-14).
#
# This replaced MIDWEEK_TRADING_CLOSE = 21:00, which was Master Policy
# v1.3 §1.8's Wednesday/Thursday RESTAURANT trading hours being used as a
# client's function curfew. tests/test_documents.py has recorded the
# distinction since the same figure was deleted from the hire agreement:
# "Restaurant trading hours (12pm-9pm Wed/Thu) must not read as the
# client's function curfew -- functions are licensed to midnight."
LICENSED_CLOSE = dt.time(0, 0)


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
                message=f"Saturday daytime functions must finish by {_clock(DAYTIME_CUTOFF)} — this booking ends at {_clock(end_time)}.",
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


def validate_setup_access_time(requested_time: dt.time) -> list[ValidationWarning]:
    """Never rejects -- earlier-than-standard access is a request Aaron
    must confirm, not something this validator can approve or deny."""
    if requested_time < SETUP_ACCESS_STANDARD_TIME:
        return [
            ValidationWarning(
                code="setup_access_requires_confirmation",
                message=(
                    f"Setup access from {requested_time.strftime('%I:%M%p').lstrip('0').lower()} is earlier than "
                    "the 2:00pm standard — this must be confirmed by Aaron, never promised automatically."
                ),
            )
        ]
    return []


def validate_trading_hours(
    event_date: dt.date, end_time: dt.time, start_time: dt.time | None = None
) -> list[ValidationWarning]:
    """The function licence: midnight, every night.

    This used to warn that Wednesday and Thursday close at 9:00pm, which is
    the restaurant's hours rather than the licence, and produced a warning
    on ordinary midweek functions that run past 9pm as a matter of course.

    A finish PAST MIDNIGHT is the thing that actually breaches the licence,
    and it is visible only as an end time that falls before the start --
    the column holds a time of day with no date, so 12:30am and 12:30pm are
    told apart by which side of the start they land on. With no start_time
    to compare against there is nothing this can conclude, and it says
    nothing rather than guessing.

    Every night, not midweek: the licence does not vary by day, so neither
    does this.
    """
    if start_time is None or end_time > start_time:
        return []
    return [
        ValidationWarning(
            code="finish_after_licensed_close",
            message=(
                "This booking proposes finishing at "
                f"{end_time.strftime('%I:%M%p').lstrip('0').lower()}, after midnight — the venue is "
                "licensed until midnight, so a later finish has to be confirmed."
            ),
        )
    ]
