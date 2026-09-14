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
from app.services.document_generation import NO_DIETARIES, bar_structure_shown
from app.services.document_regeneration import unfilled_fields as _unfilled_fields
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


# --- venue identity, per render ---------------------------------------------


def venue_identity(venue) -> dict:
    """The venue facts a client-facing template prints, for THIS venue.

    These were Jinja GLOBALS until 2026-09-12: bound once at import from
    module constants, which is process-wide by definition and therefore had
    exactly one answer. The Entrance is a different company with its own
    ABN, bank account and Stripe account, so one answer is the wrong number
    of answers.

    Returns a plain dict so every call site passes it explicitly. There is
    deliberately NO fallback to Hamilton: a render that forgets the venue
    gets empty strings and shows a blank ABN, which somebody notices. A
    fallback would print another company's real bank details on this
    company's invoice, which nobody notices until the money lands in the
    wrong account.

    `venue` may be None for a page with no booking behind it (an expired
    link, an error page). Empty is the right answer there too.

    EVERY key is prefixed `venue_`, without exception. Three of them were not
    until 2026-09-12 -- bank_account_name, bank_bsb, bank_account_number --
    and Jinja renders an unknown key as an empty string with no error, so
    somebody writing `venue_bank_bsb` (the name the other seven make natural)
    would have got a BLANK BANK BLOCK on a client invoice while the ABN
    directly above it stayed correct. Three fields empty and one right reads
    as a template bug, not as a missing record, so it would have been
    debugged rather than fixed. Keep the prefix on anything added here, and
    see test_every_identity_key_a_template_reads_is_actually_supplied.
    """
    if venue is None:
        return {key: "" for key in _IDENTITY_KEYS}
    return {
        "venue_trading_name": venue.trading_name or "",
        "venue_legal_name": venue.legal_name or "",
        "venue_abn": venue.abn or "",
        "venue_address": venue.address or "",
        "venue_phone": venue.phone or "",
        "venue_contact_name": venue.contact_name or "",
        "venue_contact_email": venue.contact_email or "",
        "venue_bank_account_name": venue.bank_account_name or "",
        "venue_bank_bsb": venue.bank_bsb or "",
        "venue_bank_account_number": venue.bank_account_number or "",
    }


_IDENTITY_KEYS = (
    "venue_trading_name",
    "venue_legal_name",
    "venue_abn",
    "venue_address",
    "venue_phone",
    "venue_contact_name",
    "venue_contact_email",
    "venue_bank_account_name",
    "venue_bank_bsb",
    "venue_bank_account_number",
)


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
def beo_billing(document) -> dict:
    """The Event Order's money block, computed NOW from what has been paid.

    Returns {"total", "deposit_paid", "balance_due"} as strings, or None
    values where the figure genuinely is not knowable.

    LIVE, NOT FROZEN, and deliberately so. The stored block was written
    when the document was generated, so a deposit paid afterwards never
    reached it -- and the Generate path never passed a deposit at all, so
    every Event Order made outside the wizard printed "[REVIEW] payments
    aren't tracked in Concierge until Phase 3" over a payment the invoices
    table was holding. Reading it here fixes every document that already
    exists, with no regeneration: the same mechanism, and the same reason,
    as the unfilled-fields block below.

    The session comes from the document itself. During any render it is
    ORM-attached, so there is no route wiring to forget at one of the six
    places that render this template -- the template asks and this answers.
    With no session (a detached object in a test or a script) it falls
    back to the stored block, which is never worse than today.
    """
    content = document.content or {}
    stored = content.get("total_food_spend") or {}

    # The food total stays derived from the document's OWN stored lines:
    # what was ordered is a fact about this version of the Event Order, and
    # it is what the client agreed to. Only the PAYMENT side is live.
    total = stored.get("total")

    from decimal import Decimal
    from sqlalchemy.orm import object_session

    db = object_session(document)
    if db is None:
        return {
            "total": total,
            "deposit_paid": stored.get("deposit_paid"),
            # The stored block never carried a total; the deposit is the
            # best a detached object can say, and it is never worse than
            # the frozen figure this branch already returns beside it.
            "total_paid": stored.get("deposit_paid"),
            "balance_due": stored.get("balance_due"),
        }

    from sqlalchemy import select

    from app.models import Invoice
    from app.services import invoicing

    deposit_paid = invoicing.get_deposit_paid(db, document.booking)
    # EVERY payment on the booking, not just the deposit. "Total paid" and
    # "Balance owing" printed deposit_paid, so a part payment against the
    # FINAL invoice -- money the client had handed over -- was invisible on
    # their own Event Order: paid understated, owing overstated, on the
    # screen and the PDF. The "Less deposit paid" bullet keeps deposit_paid;
    # that line is about the deposit. These two are about everything.
    total_paid = sum(
        (
            invoicing.get_total_paid(db, inv.id)
            for inv in db.scalars(
                select(Invoice).where(Invoice.booking_id == document.booking_id)
            ).all()
        ),
        Decimal("0.00"),
    )
    balance_due = None
    if total is not None:
        balance_due = Decimal(str(total)) - total_paid

    return {
        "total": total,
        # 0.00 is a FACT, not an unknown -- nobody has paid a deposit yet,
        # which is a different statement from "we cannot tell". The same
        # distinction beo_proposals._deposit_paid_for records.
        "deposit_paid": f"{deposit_paid:.2f}",
        "total_paid": f"{total_paid:.2f}",
        "balance_due": f"{balance_due:.2f}" if balance_due is not None else None,
    }


templates.env.filters["line_total"] = line_total
# The Bar Structure section prints the credit above the words. Joined at
# render time so the stored field stays free of generated text -- see
# document_generation.bar_structure_shown.
templates.env.filters["bar_structure_shown"] = bar_structure_shown
# The staff copy names the fields nobody has filled in, so a lost value and
# a value never entered stop looking identical on the page. Computed at
# render time on purpose: it works on every document that already exists,
# without regenerating any of them.
templates.env.filters["unfilled_fields"] = _unfilled_fields
# The Event Order's deposit and balance, read at render from what has
# actually been paid rather than from a figure frozen at generation. A
# filter rather than six route contexts: one forgotten render site
# would print a dash where money goes.
def beo_timeline_bullets(document) -> list:
    """The Event Order's timeline bullets, with each vendor's bump-in
    requested/confirmed qualifier read AT RENDER from the vendor rows.

    The bullets are composed once by build_event_timeline and frozen into
    the document. _refresh_draft_beo_timeline re-composes them when a
    bump-in is confirmed -- but only on a DRAFT, so an Event Order that has
    already gone out goes on saying "requested -- not yet confirmed" after
    staff have confirmed the time in writing. Adam Williams' DJ was
    confirmed on 2 September and his Event Order still said requested
    twelve days later, while the Music field on the same page said
    otherwise. One document, two answers.

    Same mechanism and same reason as beo_billing above: computed here, so
    it is true of every document that already exists without regenerating
    any of them, and there is no route context for six render sites to
    forget.

    ONLY the qualifier moves. The bullet's time, vendor name and contact
    stay exactly as they were composed -- this is not a rebuild of the
    timeline, which would discard anything a person typed into it.
    """
    content = document.content or {}
    bullets = list((content.get("event_timeline") or {}).get("bullets") or [])

    from sqlalchemy.orm import object_session

    db = object_session(document)
    if db is None:
        return bullets

    booking = document.booking
    confirmed_now = {
        v.name for v in booking.vendors if v.bump_in_confirmed and v.bump_in_time is not None
    }
    if not confirmed_now:
        return bullets

    out = []
    for line in bullets:
        # Matched on the frozen wording build_vendor_snapshot composes, and
        # on the vendor's own name -- so a bullet for a vendor still
        # genuinely unconfirmed is left alone.
        if "(requested — not yet confirmed)" in line and any(name in line for name in confirmed_now):
            line = line.replace("(requested — not yet confirmed)", "(confirmed)")
        out.append(line)
    return out


templates.env.filters["beo_billing"] = beo_billing
# The bump-in qualifier, read at render from booking_vendors rather than
# the copy frozen when the Event Order was generated.
templates.env.filters["beo_timeline_bullets"] = beo_timeline_bullets
from app.config import settings as _settings  # noqa: E402

# Browser tags render only in production (or locally, where the ids are
# empty anyway): the ids alone are not the switch, the environment is too.
_tracking_env_ok = (_settings.railway_environment_name or "").strip().lower() in ("", "production")
templates.env.globals["ga4_measurement_id"] = _settings.ga4_measurement_id if _tracking_env_ok else ""
templates.env.globals["meta_pixel_id"] = _settings.meta_pixel_id if _tracking_env_ok else ""
templates.env.globals["has_venue_logo"] = has_venue_logo
templates.env.globals["venue_logo_url"] = LOGO_STATIC_PATH
# The Dietaries section prints this whatever the stored value is -- the
# template had its own literal copy, so changing the constant would have
# left the two disagreeing about the one sentence a client reads for an
# allergy question. document_regeneration reads the same name.
templates.env.globals["NO_DIETARIES"] = NO_DIETARIES

# Venue identity is NO LONGER a global. It was ten of them, bound here once
# at import from module constants -- process-wide by definition, and
# therefore one answer for a system that now needs two. Every client-facing
# render passes templating.venue_identity(venue) instead.
#
# Live (not frozen) is still the rule for the INVOICE: an unpaid invoice
# must point at the account that is current now, not whatever was true when
# it was generated. Signed agreements deliberately do the opposite (see
# app.services.document_generation), freezing the same facts into the
# document's content, because a contract reflects what was agreed.
#
# venue_phone stays below only because document.html reads it on the Event
# Order header; it is supplied per render as well and the global is gone.
