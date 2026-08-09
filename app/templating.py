"""Shared Jinja2Templates instance for the public-facing views.

Centralized so the sydney_time filter is registered once and used
consistently everywhere a timestamp is shown to a client -- not
duplicated (and potentially forgotten) per router.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

# Every other timestamp in this system (event_date, start_time, etc.) is
# already local Sydney time by design (see Booking.time_range). Timestamp
# columns that use DateTime(timezone=True) (signed_at, paid_at) are
# stored UTC-aware, which is correct for storage -- but rendering them
# with a raw .strftime() in a template displays UTC, not what the client
# actually experienced. A client who signed at 9pm Saturday should not
# see "Saturday" turn into "Sunday" because the server printed UTC.
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def sydney_time(value: dt.datetime | None, fmt: str = "%d %b %Y at %I:%M%p") -> str:
    if value is None:
        return ""
    return value.astimezone(SYDNEY_TZ).strftime(fmt)


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["sydney_time"] = sydney_time
