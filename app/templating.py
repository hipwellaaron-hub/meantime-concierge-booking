"""Shared Jinja2Templates instance for the public-facing views.

Centralized so the sydney_time filter is registered once and used
consistently everywhere a timestamp is shown to a client -- not
duplicated (and potentially forgotten) per router.
"""

import datetime as dt
import os
from zoneinfo import ZoneInfo

import markupsafe
from fastapi.templating import Jinja2Templates

from app.services import policy

# The venue wordmark, shown on client-facing documents. Optional on
# purpose: if the file isn't present the templates fall back to the venue
# name set as type, so a missing asset degrades to something that still
# looks deliberate rather than a broken-image box on a contract.
LOGO_STATIC_PATH = "/static/logo.png"
LOGO_FILE_PATH = os.path.join("app", "static", "logo.png")

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


# Roughly how many characters of clause body fit on one line in a single
# column of the two-column print layout, and how many line-heights the
# heading plus its spacing costs. Both are estimates -- the point is only
# to split the clauses into two columns of similar height, which does not
# need to be exact to look right.
_PRINT_CHARS_PER_LINE = 68
_PRINT_HEADING_LINES = 2.1


def _estimated_lines(section: dict) -> float:
    """Rendered height of one clause, in line-heights. Counts hard line
    breaks separately rather than dividing the whole body by the line
    width: the 18th birthday conditions are a bulleted list of short
    lines, so on raw character count they look about half as tall as they
    actually render."""
    lines = _PRINT_HEADING_LINES
    for paragraph in section.get("body", "").split("\n"):
        lines += max(1, -(-len(paragraph) // _PRINT_CHARS_PER_LINE))
    return lines


def balance_columns(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split terms clauses into two roughly equal-height columns for the
    print layout. Split on estimated rendered height, not clause count:
    the clauses vary enormously (Guest Numbers runs several times the
    length of Credit Card), so halving by count strands one column
    part-way up the page."""
    if not sections:
        return [], []
    heights = [_estimated_lines(s) for s in sections]
    total = sum(heights)

    # Cut where the running height first gets closest to half. Compares
    # the gap on either side of each candidate cut rather than stopping
    # at the first one past halfway, which otherwise consistently
    # overfills the left column by most of one clause.
    best_index, best_gap, running = 1, None, 0.0
    for index, height in enumerate(heights[:-1]):
        running += height
        gap = abs(total - 2 * running)
        if best_gap is None or gap < best_gap:
            best_gap, best_index = gap, index + 1
    return sections[:best_index], sections[best_index:]


def has_venue_logo() -> bool:
    """Checked per render, not once at import: the logo is a deploy-time
    asset, and a stale cached False would silently keep it off every
    document until the next restart."""
    return os.path.exists(LOGO_FILE_PATH)


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["sydney_time"] = sydney_time
templates.env.filters["nl2br"] = nl2br
templates.env.filters["balance_columns"] = balance_columns
templates.env.globals["has_venue_logo"] = has_venue_logo
templates.env.globals["venue_logo_url"] = LOGO_STATIC_PATH

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
