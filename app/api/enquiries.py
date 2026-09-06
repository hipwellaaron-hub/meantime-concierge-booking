import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Venue
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services.attribution import build_touch, parse_attribution_payload, reconcile_touches, touches_from_cookies
from app.services import conversions, drafting
from app.services.enquiry_classification import create_enquiry_booking
from app.services.lead_analytics import classify_lead_source
from app.templating import templates
from app.utils import truncate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enquiries"])

# Generous for a real person (even re-submitting after fixing a typo
# shouldn't realistically hit this within 5 minutes), restrictive enough
# to blunt a scripted flood of fake enquiries.
_enquiry_rate_limiter = InMemoryRateLimiter(max_requests=5, window_seconds=300)
# The conversion beacon is unauthenticated and writes a row; a real
# browser sends at most two per enquiry.
_beacon_rate_limiter = InMemoryRateLimiter(max_requests=30, window_seconds=300)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _submission_uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except (ValueError, AttributeError, TypeError):
        return None


def _venue(db: Session) -> Venue:
    # Single-venue app -- same default used throughout (see
    # app/api/availability.py, app/api/admin_dashboard.py).
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("/enquire", response_class=HTMLResponse)
def enquiry_form(request: Request):
    return templates.TemplateResponse(request, "enquiry.html", {"event_types": EVENT_TYPES})


@router.post("/enquiries", dependencies=[Depends(rate_limit_dependency(_enquiry_rate_limiter))])
def submit_enquiry(
    request: Request,
    payload: Annotated[EnquiryCreate, Form()],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    venue = _venue(db)
    full_name = truncate(f"{payload.first_name} {payload.last_name}", 255)
    referrer = request.headers.get("referer")
    lead_source = classify_lead_source(payload.lead_source, referrer)
    actor = truncate(f"public_enquiry:{payload.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)
    first_touch, last_touch = parse_attribution_payload(payload.attribution, fallback_referrer=referrer)
    # A genuine public web enquiry always carries a real attribution bundle
    # (an "unknown" one when there's no ad signal), which is what marks it
    # as conversion-eligible vs a staff-entered/imported booking (NULL). The
    # only way parse returns None is an API/no-referrer edge; floor it to a
    # real bundle so the thank-you page can reliably tell the two apart.
    if first_touch is None:
        first_touch = build_touch({"referrer": referrer})
    if last_touch is None:
        last_touch = first_touch
    # The parent-domain cookies the website's tags set are visible on this
    # POST: Google's linker cookie carries the gclid of a click that landed
    # on meantime.com.au, Meta's carries the fbclid, and the website's own
    # mt_touch_* cookies carry UTMs. Each is its own touch, ordered by time.
    # Tracking must never block an enquiry: any parse failure means "no
    # cookie touches", never an error.
    try:
        first_touch, last_touch = reconcile_touches(first_touch, last_touch, touches_from_cookies(request.cookies))
    except Exception:  # noqa: BLE001
        logger.exception("Cookie attribution failed; enquiry continues without it")
    try:
        tracking_context = conversions.build_tracking_context(
            cookies=request.cookies, user_agent=request.headers.get("user-agent"), client_ip=client_ip(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Tracking context failed; enquiry continues without it")
        tracking_context = None
    submission_id = _submission_uuid(payload.submission_id)

    enquiry_kwargs = dict(
        venue=venue,
        full_name=full_name,
        email=payload.email,
        phone=payload.phone,
        event_name=payload.event_name,
        event_type=payload.event_type,
        event_date=payload.event_date,
        proposed_time_slot=payload.proposed_time_slot,
        attendee_count=payload.attendee_count,
        adult_count=payload.adult_count,
        company_name=payload.company_name,
        dates_flexible=payload.dates_flexible,
        comments=payload.comments,
        lead_source=lead_source,
        lead_referrer=referrer,
        actor=actor,
        first_touch_attribution=first_touch,
        last_touch_attribution=last_touch,
        tracking_context=tracking_context,
    )
    try:
        booking, _duplicate_candidates, _is_new = create_enquiry_booking(db, submission_id=submission_id, **enquiry_kwargs)
    except IntegrityError:
        # The one race the email lock cannot serialise: the same
        # submission_id arriving at the same moment under two different
        # emails. The second insert trips the unique index; it is a real
        # enquiry, so it is created again without the id rather than
        # failing the client. A clean second pass redoes the lock, the
        # contact and the duplicate check.
        db.rollback()
        booking, _duplicate_candidates, _is_new = create_enquiry_booking(db, submission_id=None, **enquiry_kwargs)

    # The server copy of the Meta Lead goes after the response too, with
    # the same event id as the browser copy so Meta counts one. GA4's
    # server copy is a delayed fallback handled by the sweep, never here.
    if _is_new:
        background_tasks.add_task(conversions.dispatch_after_enquiry, booking.id)

    # Drafting runs AFTER this response is sent, in its own session, and
    # cannot raise into the request. By this line the booking is saved,
    # flagged, and staff have been notified inside create_enquiry_booking.
    # A slow or dead model therefore costs one missing draft and nothing
    # else. Duplicate submissions (a double-click) are not re-drafted.
    if _is_new:
        background_tasks.add_task(drafting.run_in_background, booking.id)

    return RedirectResponse(url=f"/enquiries/{booking.id}/thanks", status_code=303)


# The two ad platforms Concierge fires a browser conversion to, mapped to
# the column that records when the BROWSER confirmed it dispatched each.
_DISPATCH_COLUMNS = {
    "ga4": Booking.ga4_conversion_dispatched_at,
    "meta": Booking.meta_conversion_dispatched_at,
}


def _is_public_enquiry(booking: Booking) -> bool:
    # A genuine public web enquiry always carries a real attribution bundle
    # (a dict); staff-entered / imported bookings don't and must never fire
    # an ad conversion. Checked on the loaded value, not IS NULL: a JSONB
    # column stores Python None as JSON 'null', which is NOT SQL NULL.
    return isinstance(booking.first_touch_attribution, dict)


@router.get("/enquiries/{booking_id}/thanks", response_class=HTMLResponse)
def enquiry_thanks(booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Not found")

    # Offer each platform's conversion snippet only while the browser hasn't
    # yet confirmed dispatching it. Deliberately writes NOTHING here --
    # rendering the page is not "emitted"; the browser beacons back after it
    # actually runs gtag/fbq (see the conversion beacon below). So a closed
    # tab or a blocked tag before dispatch leaves the flag NULL and the
    # snippet is offered again on a later load, while a refresh after a
    # confirmed dispatch omits it. Per platform, so one being blocked never
    # suppresses the other.
    eligible = _is_public_enquiry(booking)
    emit_ga4 = eligible and booking.ga4_conversion_dispatched_at is None
    emit_meta = eligible and booking.meta_conversion_dispatched_at is None

    return templates.TemplateResponse(
        request,
        "enquiry_submitted.html",
        {
            "booking": booking,
            "emit_ga4": emit_ga4,
            "emit_meta": emit_meta,
            "conversion_lead_id": booking.reference_code,
            # Only a value from the form's own list reaches the tag.
            "conversion_enquiry_type": conversions.safe_event_type(booking.event_type),
        },
    )


@router.post(
    "/enquiries/{booking_id}/conversion/{platform}", status_code=204,
    dependencies=[Depends(rate_limit_dependency(_beacon_rate_limiter))],
)
def record_conversion_dispatch(
    booking_id: uuid.UUID, platform: str, request: Request, db: Session = Depends(get_db)
):
    """Fire-and-forget beacon the browser sends AFTER it dispatches a
    platform's conversion (gtag / fbq). Records dispatch so a refresh /
    Back/Forward, or the same confirmation URL reopened in another browser,
    won't fire it again -- the app-level dedup that does NOT assume GA4/Meta
    deduplicate on their own. Only ever accepted for a genuine public
    enquiry, and only recorded once (atomic flip off NULL). Idempotent and
    side-effect-light on purpose: a replayed or unknown beacon is a silent
    no-op, never an error the browser has to handle."""
    column = _DISPATCH_COLUMNS.get(platform)
    if column is None:
        return Response(status_code=204)
    booking = db.get(Booking, booking_id)
    if booking is None or not _is_public_enquiry(booking):
        return Response(status_code=204)

    result = db.execute(
        update(Booking).where(Booking.id == booking_id, column.is_(None)).values({column: dt.datetime.now(dt.timezone.utc)})
    )
    conversions.record_browser_dispatch(db, booking, platform)
    db.commit()
    if result.rowcount == 1:
        logger.info("%s conversion dispatched by browser for enquiry %s", platform, booking.reference_code)
    return Response(status_code=204)
