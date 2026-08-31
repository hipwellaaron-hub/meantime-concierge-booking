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
from app.utils import format_date_dmy as _format_date_dmy
from app.utils import format_person_name as _format_person_name

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


REVIEW_MARKER = "[REVIEW]"
CLIENT_PLACEHOLDER = "To be confirmed — contact the venue"


def client_safe(value, staff: bool = False, placeholder: str = CLIENT_PLACEHOLDER):
    """[REVIEW] markers are staff-facing prompts baked into generated
    document content (see app.services.document_generation). They must
    never reach a client: an instruction to staff printed on a contract or
    Event Order reads as sloppiness at best. Staff surfaces pass
    staff=True and see the raw flagged text; every other surface (the
    public /d/{token} view and the PDF) gets a neutral placeholder."""
    if value is None or staff:
        return value
    if isinstance(value, str) and REVIEW_MARKER in value:
        return placeholder
    return value


def bullets(value) -> list[str]:
    """Newline-separated builder output -> a list of bullet lines. The
    Event Order renders everything as bullets, never paragraphs; old
    prose content simply becomes a single bullet."""
    if not value:
        return []
    return [line.strip() for line in str(value).split("\n") if line.strip()]


def line_total(item: dict) -> str:
    """"$300" for a 3 x $100.00 line -- the reference document shows line
    totals, whose absence made a qty-2 line at unit $250 look like it
    contradicted a $500 total."""
    from decimal import Decimal, InvalidOperation

    try:
        total = Decimal(str(item.get("quantity", 0))) * Decimal(str(item.get("unit_price", "0")))
    except (InvalidOperation, TypeError):
        return ""
    if total == total.to_integral_value():
        return f"${total:,.0f}"
    return f"${total:,.2f}"


def has_venue_logo() -> bool:
    """Checked per render, not once at import: the logo is a deploy-time
    asset, and a stale cached False would silently keep it off every
    document until the next restart."""
    return os.path.exists(LOGO_FILE_PATH)


# One place that decides the colour of a status pill, so every list --
# bookings, invoices, documents, wizard -- reads the same way: green =
# done/won/paid, gold = live/in-progress, wine = dead/cancelled, and a
# plain neutral pill for a brand-new enquiry. Covers the status values of
# every enum in the app; an unknown value falls back to neutral.
_STATUS_BADGE_CLASSES = {
    # good / settled
    "confirmed": "green", "completed": "green", "paid": "green", "signed": "green", "submitted": "green",
    # live / in progress
    "offered": "gold", "tentative": "gold", "sent": "gold", "viewed": "gold", "draft": "gold", "in_progress": "gold",
    # ended unfavourably
    "cancelled": "wine", "dead": "wine", "archived": "wine", "revoked": "wine",
    # brand new -- neutral
    "enquiry": "",
}


def status_badge(value) -> str:
    """The badge colour class for a status value (an enum member or its
    string). Used as `class="badge {{ x.status.value | status_badge }}"`."""
    if value is None:
        return ""
    key = getattr(value, "value", value)
    return _STATUS_BADGE_CLASSES.get(str(key), "")


def time_ago(value: dt.datetime | None) -> str:
    """A short relative time ('2h ago', '3d ago') for at-a-glance lists.
    The exact timestamp still belongs in a title attribute alongside it, so
    hovering gives the precise time -- this is the scannable summary, not a
    replacement for the real value."""
    if value is None:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    moment = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    delta = now - moment
    secs = delta.total_seconds()
    if secs < 0:
        return sydney_time(value)  # a future timestamp -- just show it plainly
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    return sydney_time(value, "%d %b %Y")


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["sydney_time"] = sydney_time
templates.env.filters["status_badge"] = status_badge
templates.env.filters["time_ago"] = time_ago
templates.env.filters["nl2br"] = nl2br
templates.env.filters["balance_columns"] = balance_columns
templates.env.filters["client_safe"] = client_safe
templates.env.filters["person_name"] = _format_person_name
templates.env.filters["aus_date"] = _format_date_dmy
templates.env.filters["bullets"] = bullets
templates.env.filters["line_total"] = line_total
templates.env.globals["has_venue_logo"] = has_venue_logo
templates.env.globals["venue_logo_url"] = LOGO_STATIC_PATH
templates.env.globals["venue_phone"] = policy.VENUE_PHONE

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
