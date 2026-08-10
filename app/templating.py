"""Shared Jinja2Templates instance for the public-facing views.

Centralized so the sydney_time filter is registered once and used
consistently everywhere a timestamp is shown to a client -- not
duplicated (and potentially forgotten) per router.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import markupsafe
from fastapi.templating import Jinja2Templates

from app.services import policy

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


def nl2br(value: str) -> markupsafe.Markup:
    """Renders embedded newlines as real <br> tags. CSS white-space:
    pre-wrap is not reliably honoured by xhtml2pdf (used for PDF
    downloads -- see app.services.pdf), so multi-paragraph content like a
    generated agreement's terms_text needs actual line-break markup to
    render correctly in both the HTML view and the PDF, not just CSS."""
    escaped = markupsafe.escape(value)
    return markupsafe.Markup("<br>\n").join(escaped.split("\n"))


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["sydney_time"] = sydney_time
templates.env.filters["nl2br"] = nl2br

# Live (not frozen) venue/banking details for the invoice view -- an unpaid
# invoice must always point at the current account, not whatever was true
# when it was generated. Signed agreements deliberately do the opposite
# (see app.services.document_generation): those freeze this same data into
# the document's content at generation time, because a contract has to
# reflect what was true when it was agreed, not what's true today.
templates.env.globals["venue_trading_name"] = policy.VENUE_TRADING_NAME
templates.env.globals["venue_legal_name"] = policy.VENUE_LEGAL_NAME
templates.env.globals["venue_abn"] = policy.VENUE_ABN
templates.env.globals["venue_address"] = policy.VENUE_ADDRESS
templates.env.globals["bank_account_name"] = policy.BANK_ACCOUNT_NAME
templates.env.globals["bank_bsb"] = policy.BANK_BSB
templates.env.globals["bank_account_number"] = policy.BANK_ACCOUNT_NUMBER
templates.env.globals["venue_contact_name"] = policy.VENUE_CONTACT_NAME
templates.env.globals["venue_contact_email"] = policy.VENUE_CONTACT_EMAIL
