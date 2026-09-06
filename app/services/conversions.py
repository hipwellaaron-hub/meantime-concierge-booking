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
   must never run alongside a browser send that succeeded. The residual
   case -- the browser fired but its confirmation beacon was lost -- is a
   documented limitation, not something this code can see.

Every attempt is recorded in conversion_dispatches so a failure is
retried by the sweep and a success is reconcilable against the account.

Never PII: the payloads carry the booking reference, venue, source system
and enquiry type. Meta's user_data carries the browser ids Meta itself
set (_fbp/_fbc), the user agent and the client address, which is what the
Conversions API needs to match a browser; no name, email or phone, and
nothing hashed from them.

Secrets stay in settings and are never logged, never stored, never put in
a URL where a log could catch them (the Meta token travels in the JSON
body; the GA4 api_secret has to be a query parameter by Google's design,
so failures record the exception class and the status, never the URL).
"""

import datetime as dt
import logging
import re
import uuid
from typing import Mapping

import httpx
from sqlalchemy import select
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

logger = logging.getLogger(__name__)

META_GRAPH_VERSION = "v21.0"
GA4_MP_URL = "https://www.google-analytics.com/mp/collect"
GA4_MP_DEBUG_URL = "https://www.google-analytics.com/debug/mp/collect"

EVENT_NAME_GA4 = "function_enquiry_submitted"
EVENT_NAME_META = "Lead"
VENUE_PARAM = "hamilton"
SOURCE_SYSTEM = "meantime_concierge"

# A conversion older than this is not sent from the server at all: GA4
# rejects events more than 72 hours old, Meta accepts up to 7 days, and a
# lead that old has been handled by a human anyway.
MAX_AGE = dt.timedelta(days=7)
GA4_MAX_AGE = dt.timedelta(hours=72)

REQUEST_TIMEOUT_SECONDS = 10.0

_GA_COOKIE = re.compile(r"^GA1\.\d+\.(\d+\.\d+)$")
_GA_SESSION = re.compile(r"\$?s(\d{6,})")


# --- what the server can see on the POST -------------------------------------


def build_tracking_context(*, cookies: Mapping[str, str], user_agent: str | None, client_ip: str | None) -> dict:
    """The browser identifiers a server-side conversion needs to be matched
    to the same visitor: read from the parent-domain cookies the website's
    and Concierge's own tags set. All optional; a visitor with no cookies
    yields an empty-ish context and the server sends what it can."""
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
        if value and len(value) <= 200:
            context[cookie.lstrip("_")] = value
    if user_agent:
        context["user_agent"] = user_agent[:500]
    if client_ip:
        context["client_ip"] = client_ip[:64]
    return context


def client_ip_from_headers(headers: Mapping[str, str], fallback: str | None) -> str | None:
    forwarded = headers.get("x-forwarded-for") or ""
    first = forwarded.split(",")[0].strip()
    return first or fallback


# --- enablement ----------------------------------------------------------------


def server_dispatch_enabled() -> bool:
    """The production opt-in. Staging and local never set it, so a copy of
    the production database on another host cannot send real conversions."""
    return bool(settings.tracking_server_dispatch_enabled)


def meta_enabled() -> bool:
    return server_dispatch_enabled() and bool(settings.meta_pixel_id) and bool(settings.meta_capi_access_token)


def ga4_enabled() -> bool:
    return server_dispatch_enabled() and bool(settings.ga4_measurement_id) and bool(settings.ga4_api_secret)


def is_public_enquiry(booking: Booking) -> bool:
    return isinstance(booking.first_touch_attribution, dict)


# --- the record ----------------------------------------------------------------


def _get_or_create(db: Session, booking: Booking, platform: str, channel: str) -> ConversionDispatch:
    row = db.scalar(
        select(ConversionDispatch).where(
            ConversionDispatch.booking_id == booking.id,
            ConversionDispatch.platform == platform,
            ConversionDispatch.channel == channel,
        )
    )
    if row is None:
        row = ConversionDispatch(
            booking_id=booking.id, platform=platform, channel=channel,
            event_id=booking.reference_code, status=STATUS_SKIPPED, attempts=0,
        )
        db.add(row)
        db.flush()
    return row


def record_browser_dispatch(db: Session, booking: Booking, platform: str) -> ConversionDispatch:
    """The beacon said the browser fired this platform's tag. Recorded once;
    a replayed beacon leaves the first record untouched."""
    row = _get_or_create(db, booking, platform, CHANNEL_BROWSER)
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
            "venue": VENUE_PARAM,
            "source_system": SOURCE_SYSTEM,
            "content_category": booking.event_type,
        },
    }
    payload = {"data": [event], "access_token": settings.meta_capi_access_token}
    if settings.meta_capi_test_event_code:
        # Test Events only: the event shows in Events Manager's test tab and
        # is NOT counted as a production conversion. This is how a
        # controlled live test is run without touching the account's numbers.
        payload["test_event_code"] = settings.meta_capi_test_event_code
    return payload


def dispatch_meta(db: Session, booking: Booking, *, now: dt.datetime | None = None) -> ConversionDispatch:
    now = now or dt.datetime.now(dt.timezone.utc)
    row = _get_or_create(db, booking, PLATFORM_META, CHANNEL_SERVER)
    if row.status in (STATUS_ACCEPTED, STATUS_SENT):
        return row
    if not is_public_enquiry(booking):
        return _skip(row, "not a public enquiry")
    if not meta_enabled():
        return _skip(row, "server dispatch disabled or Meta token not configured")
    if booking.created_at and now - booking.created_at > MAX_AGE:
        return _skip(row, "enquiry older than the 7-day window")
    if row.attempts >= MAX_ATTEMPTS:
        return row

    row.attempts += 1
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{settings.meta_pixel_id}/events"
    try:
        response = httpx.post(url, json=meta_payload(booking), timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        _mark_failed(row, f"transport: {exc.__class__.__name__}", now)
        return row
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        received = int(body.get("events_received") or 0)
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
    # Never store the body verbatim: an auth error can echo the request.
    detail = ""
    try:
        detail = str((response.json().get("error") or {}).get("message") or "")[:200]
    except ValueError:
        pass
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
        "venue": VENUE_PARAM,
        "source_system": SOURCE_SYSTEM,
        "enquiry_type": booking.event_type,
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


def dispatch_ga4_fallback(db: Session, booking: Booking, *, now: dt.datetime | None = None, validate_only: bool = False) -> ConversionDispatch:
    """Send the GA4 event from the server ONLY when the browser has not
    confirmed its own send. Marks ga4_conversion_dispatched_at so the
    thank-you page stops offering the browser copy afterwards."""
    now = now or dt.datetime.now(dt.timezone.utc)
    row = _get_or_create(db, booking, PLATFORM_GA4, CHANNEL_SERVER)
    if row.status in (STATUS_ACCEPTED, STATUS_SENT):
        return row
    if not is_public_enquiry(booking):
        return _skip(row, "not a public enquiry")
    if booking.ga4_conversion_dispatched_at is not None:
        return _skip(row, "browser already confirmed the GA4 send")
    if not ga4_enabled():
        return _skip(row, "server dispatch disabled or GA4 api_secret not configured")
    if booking.created_at and now - booking.created_at > GA4_MAX_AGE:
        return _skip(row, "enquiry older than GA4's 72-hour window")
    payload = ga4_payload(booking)
    if payload is None:
        return _skip(row, "no GA4 client id was present on the submission")
    if row.attempts >= MAX_ATTEMPTS:
        return row

    row.attempts += 1
    url = GA4_MP_DEBUG_URL if validate_only else GA4_MP_URL
    try:
        response = httpx.post(
            url,
            params={"measurement_id": settings.ga4_measurement_id, "api_secret": settings.ga4_api_secret},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        _mark_failed(row, f"transport: {exc.__class__.__name__}", now)
        return row
    if validate_only:
        # The debug endpoint answers 200 with validationMessages; empty means
        # the payload is well formed. Nothing is ingested.
        try:
            messages = response.json().get("validationMessages", [])
        except ValueError:
            messages = [{"description": "non-JSON response"}]
        row.receipt = {"validation_only": True, "validation_messages": messages[:5]}
        if messages:
            _mark_failed(row, "validation: " + "; ".join(m.get("description", "") for m in messages)[:400], now)
        else:
            row.status = STATUS_SENT
            row.sent_at = now
            row.last_error = None
            row.next_attempt_at = None
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


def run_sweep(db: Session, *, now: dt.datetime | None = None) -> dict:
    """The scheduled pass: retry failed Meta sends whose backoff has elapsed,
    and send the GA4 fallback for enquiries whose browser never confirmed.
    Returns counts for the log."""
    now = now or dt.datetime.now(dt.timezone.utc)
    summary = {"meta_retried": 0, "meta_accepted": 0, "ga4_fallback_sent": 0, "ga4_skipped": 0, "meta_first_send": 0}
    since = now - MAX_AGE
    candidates = db.scalars(
        select(Booking).where(Booking.created_at >= since, Booking.first_touch_attribution.isnot(None))
    ).all()
    for booking in candidates:
        if not is_public_enquiry(booking):
            continue
        # Meta: first send if never attempted (e.g. the background task
        # died), retry if failed and due.
        meta_row = db.scalar(
            select(ConversionDispatch).where(
                ConversionDispatch.booking_id == booking.id,
                ConversionDispatch.platform == PLATFORM_META,
                ConversionDispatch.channel == CHANNEL_SERVER,
            )
        )
        if meta_row is None or (
            meta_row.status == STATUS_FAILED and meta_row.next_attempt_at is not None and meta_row.next_attempt_at <= now
        ):
            first = meta_row is None
            result = dispatch_meta(db, booking, now=now)
            summary["meta_first_send" if first else "meta_retried"] += 1
            if result.status == STATUS_ACCEPTED:
                summary["meta_accepted"] += 1
        # GA4: fallback after the grace period, never before.
        if booking.ga4_conversion_dispatched_at is None and booking.created_at is not None:
            age = now - booking.created_at
            if age >= dt.timedelta(minutes=settings.ga4_server_fallback_after_minutes):
                ga4_row = db.scalar(
                    select(ConversionDispatch).where(
                        ConversionDispatch.booking_id == booking.id,
                        ConversionDispatch.platform == PLATFORM_GA4,
                        ConversionDispatch.channel == CHANNEL_SERVER,
                    )
                )
                due = ga4_row is None or (
                    ga4_row.status == STATUS_FAILED and ga4_row.next_attempt_at is not None and ga4_row.next_attempt_at <= now
                )
                if due:
                    result = dispatch_ga4_fallback(db, booking, now=now)
                    if result.status == STATUS_SENT:
                        summary["ga4_fallback_sent"] += 1
                    elif result.status == STATUS_SKIPPED:
                        summary["ga4_skipped"] += 1
    db.commit()
    return summary
