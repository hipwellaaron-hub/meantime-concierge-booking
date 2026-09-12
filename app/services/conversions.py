"""Server-side conversion dispatch for public function enquiries.

The browser path (app/templates/_tracking_conversion.html) stays primary:
it carries GA4's own session attribution for free and Meta's pixel
matching. This module adds the two things a browser cannot give:

1. A Meta Conversions API copy of the same Lead, sent from the server
   after the booking is persisted, with the SAME event_id (the booking
   reference) as the pixel copy. Meta deduplicates the two on event_id +
   event_name, so both may be sent and one is counted. This is Meta's
   recommended shape and it is what survives an ad blocker.

2. A GA4 Measurement Protocol FALLBACK, sent only when the browser has
   not confirmed its own send after a grace period. GA4 does not
   deduplicate a browser event and a Measurement Protocol event, so this
   must never run alongside a browser send that succeeded: the fallback
   CLAIMS the send atomically (the same NULL-flip the beacon uses) before
   it posts, so a thank-you page loading during the sweep stops offering
   the browser copy. The residual case -- the browser fired but its
   confirmation beacon was lost -- is a documented limitation, not
   something this code can see.

Every attempt is recorded in conversion_dispatches so a failure is
retried by the sweep and a success is reconcilable against the account.
Nothing is recorded when server dispatch is off or unconfigured: a row
is evidence of an attempt, and an enquiry that arrived before the
variables were set is still sent once they are (within the windows).

Never PII: the payloads carry the booking reference, venue, source system
and enquiry type (only a value from the form's own list). Meta's
user_data carries the browser ids Meta itself set (_fbp/_fbc), the user
agent and the client address, which is what the Conversions API needs to
match a browser; no name, email or phone, and nothing hashed from them.
The address and user agent are cleared once the sending windows have
passed (see retire_tracking_context).

Secrets stay in settings and are never logged, never stored, never put
in a URL where a log could catch them: the Meta token travels in the JSON
body; the GA4 api_secret has to be a query parameter by Google's design,
so the httpx request log (which prints full URLs at INFO) is silenced
here, and failures record the exception class and the status only.
"""

import datetime as dt
import ipaddress
import logging
import re
import uuid
from typing import Mapping

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Booking
from app.models.conversion_dispatch import (
    CHANNEL_BROWSER,
    CHANNEL_SERVER,
    MAX_ATTEMPTS,
    PLATFORM_GA4,
    PLATFORM_META,
    STATUS_ACCEPTED,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    ConversionDispatch,
)
from app.schemas.enquiry import EVENT_TYPES

logger = logging.getLogger(__name__)

# httpx logs "HTTP Request: POST <full url>" at INFO. The GA4 api_secret is
# a query parameter, so that line would put the secret in every log the
# sweep writes. Silenced for the whole process; nothing here needs it.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

META_GRAPH_VERSION = "v21.0"
GA4_MP_URL = "https://www.google-analytics.com/mp/collect"
GA4_MP_DEBUG_URL = "https://www.google-analytics.com/debug/mp/collect"

EVENT_NAME_GA4 = "function_enquiry_submitted"
EVENT_NAME_META = "Lead"
SOURCE_SYSTEM = "meantime_concierge"
OTHER_EVENT_TYPE = "other"

# A conversion older than this is not sent from the server at all: GA4
# rejects events more than 72 hours old, Meta accepts up to 7 days, and a
# lead that old has been handled by a human anyway.
MAX_AGE = dt.timedelta(days=7)
GA4_MAX_AGE = dt.timedelta(hours=72)
# How long the address and user agent are kept after that, before the
# retention sweep clears them: nothing sends them after MAX_AGE, so they
# have no purpose past it.
CONTEXT_RETENTION = MAX_AGE

REQUEST_TIMEOUT_SECONDS = 10.0

_GA_COOKIE = re.compile(r"^GA1\.\d+\.(\d+\.\d+)$")
_GA_SESSION = re.compile(r"\$?s(\d{6,})")


# --- what the server can see on the POST -------------------------------------


def _valid_ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def build_tracking_context(*, cookies: Mapping[str, str], user_agent: str | None, client_ip: str | None) -> dict:
    """The browser identifiers a server-side conversion needs to be matched
    to the same visitor: read from the parent-domain cookies the website's
    and Concierge's own tags set. All optional; a visitor with no cookies
    yields an empty-ish context and the server sends what it can. The
    address must be the one the trusted proxy reported (app.rate_limit
    .client_ip), and it is kept only if it parses as an address."""
    context: dict = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    ga = _GA_COOKIE.match((cookies.get("_ga") or "").strip())
    if ga:
        context["ga_client_id"] = ga.group(1)
    if settings.ga4_measurement_id:
        stream = cookies.get("_ga_" + settings.ga4_measurement_id.removeprefix("G-"))
        session = _GA_SESSION.search(stream or "")
        if session:
            context["ga_session_id"] = session.group(1)
    for cookie in ("_fbp", "_fbc"):
        value = (cookies.get(cookie) or "").strip()
        if value and len(value) <= 200 and value.isascii():
            context[cookie.lstrip("_")] = value
    if user_agent:
        context["user_agent"] = user_agent[:500]
    ip = _valid_ip(client_ip)
    if ip:
        context["client_ip"] = ip
    return context


def safe_event_type(value: str | None) -> str:
    """Only a value from the form's own list ever reaches an analytics
    parameter; anything else (an API client's free text) is "other"."""
    return value if value in EVENT_TYPES else OTHER_EVENT_TYPE


def _venue_slug(booking: Booking) -> str:
    """Which venue an analytics event is attributed to.

    Reads booking.venue, not booking.space.venue. Both answer the same thing
    -- the composite FK (space_id, venue_id) -> spaces(id, venue_id) makes
    disagreement unwritable -- but the booking's own column is the
    authoritative one and needs no join.

    NO "hamilton" FALLBACK. It used to return that from an `except
    AttributeError`, which would have credited a second company's booking to
    Hamilton in GA4 and Meta: revenue attributed to the wrong business, in
    the numbers used to decide where to spend. An empty string is the honest
    answer to "I could not tell", and an attribution dashboard shows a blank
    rather than a lie. In practice it cannot happen -- bookings.venue_id is
    NOT NULL -- but a defaulting line outlives the reason it was safe.
    """
    venue = getattr(booking, "venue", None)
    return getattr(venue, "slug", "") or ""


# --- enablement ----------------------------------------------------------------


def environment_allows_dispatch() -> bool:
    """The second key. Railway names its environments; anything that is
    not production (a duplicated staging environment, say) is refused
    even if the variables were copied across. Unset means local or test."""
    name = (settings.railway_environment_name or "").strip().lower()
    return name in ("", "production")


def server_dispatch_enabled() -> bool:
    """The production opt-in. Staging and local never set it, so a copy of
    the production database on another host cannot send real conversions."""
    return bool(settings.tracking_server_dispatch_enabled) and environment_allows_dispatch()


def meta_enabled() -> bool:
    return server_dispatch_enabled() and bool(settings.meta_pixel_id) and bool(settings.meta_capi_access_token)


def ga4_enabled() -> bool:
    return server_dispatch_enabled() and bool(settings.ga4_measurement_id) and bool(settings.ga4_api_secret)


def is_public_enquiry(booking: Booking) -> bool:
    return isinstance(booking.first_touch_attribution, dict)


# --- the record ----------------------------------------------------------------


def _find(db: Session, booking: Booking, platform: str, channel: str) -> ConversionDispatch | None:
    return db.scalar(
        select(ConversionDispatch).where(
            ConversionDispatch.booking_id == booking.id,
            ConversionDispatch.platform == platform,
            ConversionDispatch.channel == channel,
        )
    )


def _get_or_create(db: Session, booking: Booking, platform: str, channel: str, *, status: str) -> ConversionDispatch:
    """Insert-or-fetch that survives two callers racing for the same
    (booking, platform, channel): ON CONFLICT DO NOTHING, then select."""
    row = _find(db, booking, platform, channel)
    if row is not None:
        return row
    db.execute(
        pg_insert(ConversionDispatch)
        .values(
            id=uuid.uuid4(), booking_id=booking.id, platform=platform, channel=channel,
            event_id=booking.reference_code, status=status, attempts=0,
        )
        .on_conflict_do_nothing(constraint="uq_conversion_dispatch_booking_platform_channel")
    )
    db.flush()
    return _find(db, booking, platform, channel)


def record_browser_dispatch(db: Session, booking: Booking, platform: str) -> ConversionDispatch:
    """The beacon said the browser fired this platform's tag. Recorded once;
    a replayed or concurrent beacon leaves the first record untouched."""
    row = _get_or_create(db, booking, platform, CHANNEL_BROWSER, status=STATUS_SENT)
    if row.sent_at is None:
        row.status = STATUS_SENT
        row.attempts = 1
        row.sent_at = dt.datetime.now(dt.timezone.utc)
        row.receipt = {"note": "browser beacon; provider receipt not visible"}
    return row


def _backoff(attempts: int) -> dt.timedelta:
    # 5 min, 15, 45, 2h15, then every ~7h -- the sweep runs on a schedule,
    # so this only says "not before".
    minutes = min(5 * (3 ** max(attempts - 1, 0)), 420)
    return dt.timedelta(minutes=minutes)


def _mark_failed(row: ConversionDispatch, error: str, now: dt.datetime) -> None:
    row.status = STATUS_FAILED
    row.last_error = error[:500]
    row.next_attempt_at = now + _backoff(row.attempts) if row.attempts < MAX_ATTEMPTS else None


def _skip(row: ConversionDispatch, reason: str) -> ConversionDispatch:
    row.status = STATUS_SKIPPED
    row.last_error = reason[:500]
    row.next_attempt_at = None
    return row


def _json_dict(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# --- Meta Conversions API --------------------------------------------------------


def meta_payload(booking: Booking) -> dict:
    """The server copy of the pixel's Lead. Same event_name and event_id as
    the browser copy so Meta counts one. Nothing personal: see module doc."""
    context = booking.tracking_context or {}
    user_data = {
        key: context[src]
        for key, src in (
            ("client_ip_address", "client_ip"),
            ("client_user_agent", "user_agent"),
            ("fbp", "fbp"),
            ("fbc", "fbc"),
        )
        if context.get(src)
    }
    event_time = int((booking.created_at or dt.datetime.now(dt.timezone.utc)).timestamp())
    event = {
        "event_name": EVENT_NAME_META,
        "event_time": event_time,
        "event_id": booking.reference_code,
        "action_source": "website",
        "event_source_url": f"{getattr(settings, 'public_base_url', '') or settings.dashboard_base_url}/enquire",
        "user_data": user_data,
        "custom_data": {
            "lead_id": booking.reference_code,
            "venue": _venue_slug(booking),
            "source_system": SOURCE_SYSTEM,
            "content_category": safe_event_type(booking.event_type),
        },
    }
    payload = {"data": [event], "access_token": settings.meta_capi_access_token}
    if settings.meta_capi_test_event_code:
        # Test Events only: the event shows in Events Manager's test tab and
        # is NOT counted as a production conversion. This is how a
        # controlled live test is run without touching the account's numbers.
        payload["test_event_code"] = settings.meta_capi_test_event_code
    return payload


def dispatch_meta(db: Session, booking: Booking, *, now: dt.datetime | None = None) -> ConversionDispatch | None:
    """Returns the dispatch row, or None when nothing was attempted because
    dispatch is off or unconfigured (no row is written for that: the
    enquiry is still sent once the variables exist)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not meta_enabled():
        return None
    existing = _find(db, booking, PLATFORM_META, CHANNEL_SERVER)
    if existing is not None and existing.status in (STATUS_ACCEPTED, STATUS_SENT):
        return existing
    if not is_public_enquiry(booking):
        return _skip(_get_or_create(db, booking, PLATFORM_META, CHANNEL_SERVER, status=STATUS_SKIPPED), "not a public enquiry")
    if booking.created_at and now - booking.created_at > MAX_AGE:
        return _skip(_get_or_create(db, booking, PLATFORM_META, CHANNEL_SERVER, status=STATUS_SKIPPED), "enquiry older than the 7-day window")
    row = existing or _get_or_create(db, booking, PLATFORM_META, CHANNEL_SERVER, status=STATUS_FAILED)
    if row.attempts >= MAX_ATTEMPTS:
        return row

    row.attempts += 1
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{settings.meta_pixel_id}/events"
    try:
        response = httpx.post(url, json=meta_payload(booking), timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        _mark_failed(row, f"transport: {exc.__class__.__name__}", now)
        return row
    body = _json_dict(response)
    if response.status_code == 200:
        try:
            received = int(body.get("events_received") or 0)
        except (TypeError, ValueError):
            received = 0
        if received >= 1:
            row.status = STATUS_ACCEPTED
            row.sent_at = now
            row.last_error = None
            row.next_attempt_at = None
            row.receipt = {"events_received": received, "fbtrace_id": body.get("fbtrace_id"),
                           "test_event": bool(settings.meta_capi_test_event_code)}
            return row
        _mark_failed(row, f"HTTP 200 but events_received={received}", now)
        return row
    # Never store the provider's message text: it can echo request values
    # (an address a client forged, say). Codes and the trace id suffice.
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    detail = " ".join(
        f"{k}={error[k]}" for k in ("code", "error_subcode", "fbtrace_id") if error.get(k) is not None
    )
    _mark_failed(row, f"HTTP {response.status_code} {detail}".strip(), now)
    return row


# --- GA4 Measurement Protocol (fallback only) -----------------------------------


def ga4_payload(booking: Booking) -> dict | None:
    context = booking.tracking_context or {}
    client_id = context.get("ga_client_id")
    if not client_id:
        return None
    params = {
        "lead_id": booking.reference_code,
        "venue": _venue_slug(booking),
        "source_system": SOURCE_SYSTEM,
        "enquiry_type": safe_event_type(booking.event_type),
        "dispatch_channel": CHANNEL_SERVER,
        "engagement_time_msec": 1,
    }
    if context.get("ga_session_id"):
        params["session_id"] = context["ga_session_id"]
    created = booking.created_at or dt.datetime.now(dt.timezone.utc)
    return {
        "client_id": client_id,
        "timestamp_micros": int(created.timestamp() * 1_000_000),
        "non_personalized_ads": False,
        "events": [{"name": EVENT_NAME_GA4, "params": params}],
    }


def validate_ga4_payload(booking: Booking) -> list[dict]:
    """Google's validation endpoint: checks the shape, ingests nothing,
    writes nothing here. For the controlled test. Returns the validation
    messages (empty means well formed)."""
    payload = ga4_payload(booking)
    if payload is None:
        return [{"description": "no GA4 client id on the submission"}]
    response = httpx.post(
        GA4_MP_DEBUG_URL,
        params={"measurement_id": settings.ga4_measurement_id, "api_secret": settings.ga4_api_secret},
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return list(_json_dict(response).get("validationMessages") or [])


def _claim_ga4(db: Session, booking: Booking, now: dt.datetime) -> bool:
    """Flip ga4_conversion_dispatched_at off NULL for this booking, or fail.
    The same atomic shape the browser beacon uses, so exactly one of the
    two channels ever sends."""
    result = db.execute(
        update(Booking)
        .where(Booking.id == booking.id, Booking.ga4_conversion_dispatched_at.is_(None))
        .values(ga4_conversion_dispatched_at=now)
    )
    return result.rowcount == 1


def _release_ga4(db: Session, booking: Booking, now: dt.datetime) -> None:
    db.execute(
        update(Booking)
        .where(Booking.id == booking.id, Booking.ga4_conversion_dispatched_at == now)
        .values(ga4_conversion_dispatched_at=None)
    )


def dispatch_ga4_fallback(db: Session, booking: Booking, *, now: dt.datetime | None = None) -> ConversionDispatch | None:
    """Send the GA4 event from the server ONLY when the browser has not
    confirmed its own send, claiming the send before posting so the
    thank-you page stops offering the browser copy the moment the claim
    lands. Returns None when dispatch is off or unconfigured."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not ga4_enabled():
        return None
    existing = _find(db, booking, PLATFORM_GA4, CHANNEL_SERVER)
    if existing is not None and existing.status in (STATUS_ACCEPTED, STATUS_SENT):
        return existing
    if not is_public_enquiry(booking):
        return _skip(_get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER, status=STATUS_SKIPPED), "not a public enquiry")
    if booking.ga4_conversion_dispatched_at is not None:
        return _skip(_get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER, status=STATUS_SKIPPED), "browser already confirmed the GA4 send")
    if booking.created_at and now - booking.created_at > GA4_MAX_AGE:
        return _skip(_get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER, status=STATUS_SKIPPED), "enquiry older than GA4's 72-hour window")
    payload = ga4_payload(booking)
    if payload is None:
        return _skip(_get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER, status=STATUS_SKIPPED), "no GA4 client id was present on the submission")
    row = existing or _get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER, status=STATUS_FAILED)
    if row.attempts >= MAX_ATTEMPTS:
        return row
    if not _claim_ga4(db, booking, now):
        db.refresh(booking)
        return _skip(row, "browser confirmed the GA4 send first")

    row.attempts += 1
    try:
        response = httpx.post(
            GA4_MP_URL,
            params={"measurement_id": settings.ga4_measurement_id, "api_secret": settings.ga4_api_secret},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        _release_ga4(db, booking, now)
        _mark_failed(row, f"transport: {exc.__class__.__name__}", now)
        return row
    if response.status_code in (200, 204):
        # The Measurement Protocol never confirms ingestion; this is "sent".
        row.status = STATUS_SENT
        row.sent_at = now
        row.last_error = None
        row.next_attempt_at = None
        row.receipt = {"note": "Measurement Protocol accepts without a receipt", "http_status": response.status_code}
        booking.ga4_conversion_dispatched_at = now
        return row
    _release_ga4(db, booking, now)
    _mark_failed(row, f"HTTP {response.status_code}", now)
    return row


# --- orchestration ----------------------------------------------------------------


def dispatch_after_enquiry(booking_id: uuid.UUID) -> None:
    """BackgroundTasks entry point: the Meta server copy, straight after the
    enquiry is saved. Own session, swallows everything -- the client has
    their redirect and staff their notification by now."""
    try:
        with SessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if booking is not None:
                dispatch_meta(db, booking)
                db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Server-side Meta dispatch crashed for booking %s", booking_id)


def _due(row: ConversionDispatch | None, now: dt.datetime) -> bool:
    return row is None or (row.status == STATUS_FAILED and row.next_attempt_at is not None and row.next_attempt_at <= now)


def retire_tracking_context(db: Session, *, now: dt.datetime) -> int:
    """Clear the client address and user agent from bookings whose sending
    windows have passed. The pseudonymous cookie ids stay (they carry no
    more than the analytics platforms already hold) for reconciliation."""
    cutoff = now - MAX_AGE - CONTEXT_RETENTION
    since = cutoff - dt.timedelta(days=60)
    rows = db.scalars(
        select(Booking).where(
            Booking.created_at < cutoff, Booking.created_at >= since, Booking.tracking_context.isnot(None)
        )
    ).all()
    cleared = 0
    for booking in rows:
        context = booking.tracking_context
        if isinstance(context, dict) and ("client_ip" in context or "user_agent" in context):
            booking.tracking_context = {k: v for k, v in context.items() if k not in ("client_ip", "user_agent")}
            cleared += 1
    return cleared


def run_sweep(db: Session, *, now: dt.datetime | None = None) -> dict:
    """The scheduled pass: send a first Meta copy for any enquiry whose
    background task never ran, retry failed sends whose backoff has
    elapsed, send the GA4 fallback for enquiries whose browser never
    confirmed, and retire stale context. One booking at a time, each in
    its own transaction: a failure on one never rolls back another's
    recorded send."""
    now = now or dt.datetime.now(dt.timezone.utc)
    summary = {"meta_first_send": 0, "meta_retried": 0, "meta_accepted": 0, "ga4_fallback_sent": 0, "ga4_skipped": 0,
               "errors": 0, "context_retired": 0}
    since = now - MAX_AGE
    ids = db.scalars(
        select(Booking.id)
        .where(Booking.created_at >= since, Booking.first_touch_attribution.isnot(None))
        .order_by(Booking.created_at)
    ).all()
    grace = dt.timedelta(minutes=settings.ga4_server_fallback_after_minutes)
    for booking_id in ids:
        # Each booking inside its own savepoint: a failure rolls back that
        # booking's work only, never a send already recorded for another.
        try:
            with db.begin_nested():
                booking = db.get(Booking, booking_id)
                if booking is None or not is_public_enquiry(booking):
                    continue
                meta_row = _find(db, booking, PLATFORM_META, CHANNEL_SERVER)
                if _due(meta_row, now):
                    result = dispatch_meta(db, booking, now=now)
                    if result is not None:
                        summary["meta_first_send" if meta_row is None else "meta_retried"] += 1
                        if result.status == STATUS_ACCEPTED:
                            summary["meta_accepted"] += 1
                if booking.ga4_conversion_dispatched_at is None and booking.created_at is not None and now - booking.created_at >= grace:
                    ga4_row = _find(db, booking, PLATFORM_GA4, CHANNEL_SERVER)
                    if _due(ga4_row, now):
                        result = dispatch_ga4_fallback(db, booking, now=now)
                        if result is not None:
                            if result.status == STATUS_SENT:
                                summary["ga4_fallback_sent"] += 1
                            elif result.status == STATUS_SKIPPED:
                                summary["ga4_skipped"] += 1
            db.commit()
        except Exception:  # noqa: BLE001 -- one booking's failure must not undo another's send
            logger.exception("Conversion sweep failed for booking %s", booking_id)
            summary["errors"] += 1
    try:
        with db.begin_nested():
            summary["context_retired"] = retire_tracking_context(db, now=now)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Tracking-context retirement failed")
    return summary
