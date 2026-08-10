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

SATURDAY = 5  # datetime.date.weekday(): Monday=0 ... Sunday=6
THURSDAY = 3
DAYTIME_CUTOFF = dt.time(17, 0)
MUSIC_OFF_TIME = dt.time(23, 30)
# Master Policy v1.3 §1.8: "From 2:00pm standard. Earlier is often
# possible but must be confirmed, never promised."
SETUP_ACCESS_STANDARD_TIME = dt.time(14, 0)
# Master Policy v1.3 §1.8: Thursday trading is 12:00pm to 9:00pm.
THURSDAY_TRADING_CLOSE = dt.time(21, 0)


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


def validate_trading_hours(event_date: dt.date, end_time: dt.time) -> list[ValidationWarning]:
    """Scoped to the one rule actually specified: Thursday trading closes
    at 9:00pm. Not a general by-day trading-hours engine."""
    if event_date.weekday() == THURSDAY and end_time > THURSDAY_TRADING_CLOSE:
        return [
            ValidationWarning(
                code="thursday_finish_after_close",
                message=(
                    f"Thursday trading closes at 9:00pm — this booking proposes finishing at "
                    f"{end_time.strftime('%I:%M%p').lstrip('0').lower()} and must be flagged and confirmed."
                ),
            )
        ]
    return []
