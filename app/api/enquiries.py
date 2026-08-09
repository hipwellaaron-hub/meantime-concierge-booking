import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Space
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.schemas.enquiry import EnquiryCreate, EnquiryResponse
from app.services.booking import create_booking
from app.services.contact_matching import find_or_create_contact
from app.services.lead_analytics import classify_lead_source
from app.services.notifications import notify_new_enquiry
from app.utils import truncate

router = APIRouter(tags=["enquiries"])

# Generous for a real person (even re-submitting after fixing a typo
# shouldn't realistically hit this within 5 minutes), restrictive enough
# to blunt a scripted flood of fake enquiries.
_enquiry_rate_limiter = InMemoryRateLimiter(max_requests=5, window_seconds=300)

# A double-click (or a client retrying a slow/ambiguous response) fires two
# near-identical submissions within a second or two of each other -- this
# window is intentionally short so a genuine second enquiry made minutes
# later is never mistaken for a duplicate.
DUPLICATE_SUBMISSION_WINDOW = dt.timedelta(seconds=15)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _find_recent_duplicate(db: Session, *, contact_id, space_id, event_date, event_name) -> Booking | None:
    cutoff = dt.datetime.now(dt.timezone.utc) - DUPLICATE_SUBMISSION_WINDOW
    return db.execute(
        select(Booking)
        .where(
            Booking.contact_id == contact_id,
            Booking.space_id == space_id,
            Booking.event_date == event_date,
            Booking.event_name == event_name,
            Booking.created_at >= cutoff,
        )
        .order_by(Booking.created_at.desc())
    ).scalars().first()


@router.post(
    "/enquiries",
    response_model=EnquiryResponse,
    status_code=201,
    dependencies=[Depends(rate_limit_dependency(_enquiry_rate_limiter))],
)
def submit_enquiry(payload: EnquiryCreate, request: Request, db: Session = Depends(get_db)):
    space = db.get(Space, payload.space_id)
    if space is None or not space.is_bookable:
        raise HTTPException(status_code=422, detail="Unknown space")

    contact, duplicate_candidates = find_or_create_contact(db, payload.name, payload.email, payload.phone)

    existing = _find_recent_duplicate(
        db,
        contact_id=contact.id,
        space_id=payload.space_id,
        event_date=payload.event_date,
        event_name=payload.event_name,
    )
    if existing is not None:
        return EnquiryResponse(
            reference_code=existing.reference_code,
            booking_id=existing.id,
            contact_id=contact.id,
            possible_duplicate_contact=len(duplicate_candidates) > 0,
        )

    referrer = request.headers.get("referer")
    lead_source = classify_lead_source(payload.lead_source, referrer)
    actor = truncate(f"public_enquiry:{payload.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)

    booking = create_booking(
        db,
        space_id=payload.space_id,
        contact_id=contact.id,
        event_date=payload.event_date,
        proposed_time_slot=payload.proposed_time_slot,
        event_name=payload.event_name,
        event_type=payload.event_type,
        adult_count=payload.attendee_count,
        child_count=0,
        notes=payload.comments,
        actor=actor,
        lead_source=lead_source,
        lead_referrer=referrer,
    )

    # Nothing auto-sends: this only marks where a future draft pipeline
    # would pick up the enquiry, it does not send anything itself.
    notify_new_enquiry(booking)

    return EnquiryResponse(
        reference_code=booking.reference_code,
        booking_id=booking.id,
        contact_id=contact.id,
        possible_duplicate_contact=len(duplicate_candidates) > 0,
    )
