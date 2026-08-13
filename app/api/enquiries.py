import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Venue
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services.enquiry_classification import create_enquiry_booking
from app.services.lead_analytics import classify_lead_source
from app.templating import templates
from app.utils import truncate

router = APIRouter(tags=["enquiries"])

# Generous for a real person (even re-submitting after fixing a typo
# shouldn't realistically hit this within 5 minutes), restrictive enough
# to blunt a scripted flood of fake enquiries.
_enquiry_rate_limiter = InMemoryRateLimiter(max_requests=5, window_seconds=300)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _venue(db: Session) -> Venue:
    # Single-venue app -- same default used throughout (see
    # app/api/availability.py, app/api/admin_dashboard.py).
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("/enquire", response_class=HTMLResponse)
def enquiry_form(request: Request):
    return templates.TemplateResponse(request, "enquiry.html", {"event_types": EVENT_TYPES})


@router.post("/enquiries", dependencies=[Depends(rate_limit_dependency(_enquiry_rate_limiter))])
def submit_enquiry(request: Request, payload: Annotated[EnquiryCreate, Form()], db: Session = Depends(get_db)):
    venue = _venue(db)
    full_name = truncate(f"{payload.first_name} {payload.last_name}", 255)
    referrer = request.headers.get("referer")
    lead_source = classify_lead_source(payload.lead_source, referrer)
    actor = truncate(f"public_enquiry:{payload.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)

    booking, _duplicate_candidates, _is_new = create_enquiry_booking(
        db,
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
    )

    return RedirectResponse(url=f"/enquiries/{booking.id}/thanks", status_code=303)


@router.get("/enquiries/{booking_id}/thanks", response_class=HTMLResponse)
def enquiry_thanks(booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(request, "enquiry_submitted.html", {"booking": booking})
