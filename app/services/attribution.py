"""Ad-attribution capture for public enquiries: UTM parameters, Google's
gclid and Meta's fbclid, and a referrer-based fallback classification.

This is deliberately separate from app.services.lead_analytics
(lead_source/lead_referrer) -- that's a coarser, staff-facing bucket
already used for the iVvy-marketplace-cancellation decision. This module
is the ad-platform-level record: exact UTM parameters and click IDs,
captured client-side at first landing (see app/templates/enquiry.html)
and submitted as-was-first-captured rather than re-derived from whatever
happens to be in the URL/Referer at the moment the form is actually
submitted.

Pure data, always. Nothing anywhere in this codebase may branch pricing,
routing, classification, or policy on any value that passes through here
-- it arrives unauthenticated from the client and is trivially forgeable.
This module exists to capture, classify for *display*, and persist. Never
to decide.
"""

import base64
import datetime as dt
import json
import uuid
from collections import Counter
from typing import Mapping
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Space
from app.models.booking import BookingStatus

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
# gbraid/wbraid are Google's newer click identifiers used where gclid can't
# be set (iOS app/web privacy paths). Captured the same way as gclid so
# those paid clicks aren't silently unattributable; added to both first-
# and last-touch bundles by build_touch below.
CLICK_ID_FIELDS = ("gclid", "gbraid", "wbraid", "fbclid")

# Loose and deliberately conservative -- "referral" is the honest bucket
# for anything not clearly a search engine or social platform, rather
# than guessing further. The raw referrer is always stored alongside the
# classification specifically so refining these lists later doesn't lose
# anything: re-classification of already-captured data is just re-running
# this function against the stored raw value.
SEARCH_ENGINE_HOSTS = ("google.", "bing.", "duckduckgo.", "yahoo.", "baidu.", "yandex.", "ecosia.")
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "l.instagram.com", "lm.facebook.com",
    "t.co", "twitter.com", "x.com", "linkedin.com", "pinterest.", "tiktok.com",
)

# Column width cap for any single captured value -- these arrive
# unauthenticated from the client; a hostile submission stuffing a huge
# string into utm_campaign must not become an unbounded JSONB blob.
MAX_FIELD_LENGTH = 500


def classify_referrer(referrer: str | None) -> str:
    """"unknown" -- not "direct" -- when there is genuinely no referrer
    and no campaign parameters: a blank referrer is equally consistent
    with someone typing the URL in as it is with a real ad click whose
    referrer got stripped by the browser, an in-app webview, or a privacy
    setting. Asserting "direct" would be presenting a guess as a fact;
    "unknown" is the honest answer when there is truly nothing to go on."""
    if not referrer:
        return "unknown"
    try:
        host = urlparse(referrer).netloc.lower()
    except ValueError:
        return "referral"
    if not host:
        return "unknown"
    if any(marker in host for marker in SEARCH_ENGINE_HOSTS):
        return "search"
    if any(marker in host for marker in SOCIAL_HOSTS):
        return "social"
    return "referral"


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:MAX_FIELD_LENGTH] or None


def build_touch(raw: dict) -> dict:
    """Builds one touch bundle (first or last) from whatever the client
    captured -- raw UTM/click-id values plus a raw referrer string. Always
    returns a real bundle, even when every field is empty: that's the
    "unknown" classification below, a genuine fact about a real visitor
    who went through the public enquiry pipeline, distinct from the whole
    column being NULL for a booking that never went through it at all
    (staff-entered, iVvy-imported, phone) -- see the model's own comment
    on Booking.first_touch_attribution for why that distinction matters."""
    bundle = {field: _clean(raw.get(field)) for field in UTM_FIELDS + CLICK_ID_FIELDS}
    referrer = _clean(raw.get("referrer"))
    bundle["referrer"] = referrer
    bundle["referrer_category"] = classify_referrer(referrer)
    captured_at = raw.get("captured_at")
    bundle["captured_at"] = captured_at if isinstance(captured_at, str) else dt.datetime.now(dt.timezone.utc).isoformat()
    return bundle


def parse_attribution_payload(raw_json: str | None, *, fallback_referrer: str | None = None) -> tuple[dict | None, dict | None]:
    """Parses the hidden `attribution` field the enquiry form submits --
    JSON built client-side from what was captured at first landing and
    persisted in localStorage. Never raises: malformed or missing
    attribution data must never block a real enquiry from being created,
    it just means this one has none.

    If the payload is missing entirely (JS disabled/blocked, or a direct
    API submission with no browser involved), falls back to whatever the
    server itself can see on this single request -- the POST's own
    Referer header. That's a strictly weaker signal (it can't survive a
    multi-page browsing session or a return visit the way the client-side
    capture can), so first and last touch end up identical in that case --
    genuinely accurate, since a single request is all there is to go on.
    """
    if raw_json:
        try:
            data = json.loads(raw_json)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            def _extract(key: str) -> dict | None:
                touch = data.get(key)
                return build_touch(touch) if isinstance(touch, dict) else None

            first_touch = _extract("first_touch")
            last_touch = _extract("last_touch")
            if first_touch is not None or last_touch is not None:
                return first_touch, last_touch

    if fallback_referrer is not None:
        fallback = build_touch({"referrer": fallback_referrer})
        return fallback, fallback

    return None, None


# --- parent-domain cookies -------------------------------------------------------
#
# meantime.com.au and book.meantime.com.au share cookies scoped to the
# registrable domain. Proven live on 2026-09-06: after landing on the main
# site with ?gclid=..., the enquiry page could read _ga, _ga_<stream>,
# _gcl_au, _gcl_aw (which carries the gclid) and _fbp. So the click id
# reaches Concierge in a cookie even when the link is bare -- it was being
# ignored. UTM parameters are NOT in any Google or Meta cookie; for those
# the website sets mt_touch_first / mt_touch_last (see
# docs/tracking-handover.md, "Attribution handoff").

# Google's conversion-linker cookies: GCL.<unix seconds>.<value>.
_LINKER_COOKIES = {"_gcl_aw": "gclid", "_gcl_gb": "gbraid", "_gcl_gf": "wbraid"}
# Meta's click cookie: fb.<subdomain index>.<unix millis>.<fbclid>.
_META_CLICK_COOKIE = "_fbc"
# The website's own handoff of a full touch (base64url JSON).
_SITE_TOUCH_COOKIES = ("mt_touch_first", "mt_touch_last")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def _decode_site_touch(raw: str) -> dict | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def touches_from_cookies(cookies: Mapping[str, str]) -> list[dict]:
    """Each cookie that carries a campaign signal becomes its OWN touch
    bundle with its own captured_at. Bundles are never merged: a gclid from
    one click and UTMs from another visit stay two touches, and
    reconcile_touches orders them by time."""
    touches: list[dict] = []
    for cookie, field in _LINKER_COOKIES.items():
        raw = (cookies.get(cookie) or "").strip()
        parts = raw.split(".", 2)
        if len(parts) == 3 and parts[0] == "GCL" and parts[1].isdigit() and parts[2]:
            touches.append(build_touch({field: parts[2], "captured_at": _iso(int(parts[1]))}))
    raw = (cookies.get(_META_CLICK_COOKIE) or "").strip()
    parts = raw.split(".", 3)
    if len(parts) == 4 and parts[0] == "fb" and parts[2].isdigit() and parts[3]:
        touches.append(build_touch({"fbclid": parts[3], "captured_at": _iso(int(parts[2]) / 1000)}))
    for cookie in _SITE_TOUCH_COOKIES:
        raw = (cookies.get(cookie) or "").strip()
        if raw:
            data = _decode_site_touch(raw)
            if data and has_signal(build_touch(data)):
                touches.append(build_touch(data))
    return touches


def has_signal(bundle: dict | None) -> bool:
    """A touch worth attributing: any UTM value or click id."""
    return bool(bundle) and any(bundle.get(f) for f in UTM_FIELDS + CLICK_ID_FIELDS)


def _captured_at(bundle: dict) -> dt.datetime:
    raw = bundle.get("captured_at")
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _same_click(a: dict, b: dict) -> bool:
    return any(a.get(f) and a.get(f) == b.get(f) for f in CLICK_ID_FIELDS)


# The site whose internal navigation is not a touch. A visitor stepping
# from meantime.com.au to book.meantime.com.au with no campaign parameter
# has not been acquired again; the referral is the hop, not a source.
INTERNAL_REFERRER_HOSTS = ("meantime.com.au",)


def _is_internal_hop(bundle: dict) -> bool:
    if has_signal(bundle):
        return False
    try:
        host = urlparse(bundle.get("referrer") or "").netloc.lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in INTERNAL_REFERRER_HOSTS)


def reconcile_touches(first: dict, last: dict, extra: list[dict]) -> tuple[dict, dict]:
    """Combine the browser's captured touches with the cookie-derived ones.

    - Every real touch is a candidate: the page's own first and last
      (including an honest untagged direct return -- a visitor who comes
      back by typing the address has a last touch of "unknown", and that
      must not be overwritten by an older campaign), and each cookie touch
      with a signal.
    - The one visit that is NOT a touch is the internal hop from
      meantime.com.au to the enquiry page with no campaign parameter: that
      is navigation inside the same site, and counting it would make every
      website-referred enquiry's last touch "Referral".
    - First touch is the earliest candidate by captured_at, last the
      latest. A gclid the linker cookie recorded on the main website
      before the visitor reached Concierge is therefore the acquisition.
    - A touch never contributes fields to another touch; each bundle is
      taken whole. A cookie touch that is the same click the browser
      already captured is not a second touch.
    - With no candidates at all, the page's own bundles stand.
    """
    candidates = [b for b in (first, last) if not _is_internal_hop(b)]
    for bundle in extra:
        if has_signal(bundle) and not any(_same_click(bundle, existing) for existing in candidates):
            candidates.append(bundle)
    if not candidates:
        return first, last
    ordered = sorted(candidates, key=_captured_at)
    return ordered[0], ordered[-1]


def summarize_channel(bundle: dict | None) -> str:
    """One human-readable label for a touch bundle -- what the reporting
    breakdown groups by. gclid/fbclid take priority over everything else:
    they're the one unambiguous signal that a visit came from an actual
    paid click rather than an inferred source."""
    if not bundle:
        return "Unknown"

    if bundle.get("gclid") or bundle.get("gbraid") or bundle.get("wbraid"):
        return "Google Ads (paid)"
    if bundle.get("fbclid"):
        return "Meta Ads (paid)"

    medium = (bundle.get("utm_medium") or "").lower()
    source = (bundle.get("utm_source") or "").lower()
    if medium in ("cpc", "ppc", "paid", "paid_social", "paidsocial"):
        return f"Paid ({source})" if source else "Paid (other)"
    if source:
        return f"Campaign ({source})"
    if bundle.get("utm_campaign"):
        return "Campaign (untagged source)"

    category = bundle.get("referrer_category", "unknown")
    return {
        "search": "Organic search",
        "social": "Organic social",
        "referral": "Referral",
        "unknown": "Unknown",
    }.get(category, "Referral")


# Bookings that have actually confirmed -- not enquiries, not tentative
# holds -- since that's the commercial event ad spend is actually judged
# against. Reused by both the admin report and (in report-only form, not
# built) the future Google Ads offline-conversion upload -- see
# docs/google-ads-offline-conversions.md.
CONFIRMED_STATUSES = (BookingStatus.confirmed, BookingStatus.completed)


def current_quarter_start(today: dt.date) -> dt.date:
    quarter_index = (today.month - 1) // 3
    return dt.date(today.year, quarter_index * 3 + 1, 1)


def get_channel_breakdown(
    db: Session,
    venue_id: uuid.UUID,
    *,
    since: dt.date,
    until: dt.date,
    statuses: tuple[BookingStatus, ...] | None = None,
    touch: str = "first",
) -> dict[str, int]:
    """Counts bookings created in [since, until) by channel (see
    summarize_channel), based on either first or last touch. Ranked by
    volume, highest first -- this is what answers "how many confirmed
    bookings this quarter came from paid search" directly, without
    exporting anything."""
    column = Booking.first_touch_attribution if touch == "first" else Booking.last_touch_attribution
    stmt = (
        select(column)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == venue_id,
            Booking.created_at >= since,
            Booking.created_at < until,
        )
    )
    if statuses:
        stmt = stmt.where(Booking.status.in_(statuses))

    counter: Counter[str] = Counter()
    for bundle in db.scalars(stmt).all():
        counter[summarize_channel(bundle)] += 1
    return dict(sorted(counter.items(), key=lambda kv: -kv[1]))
