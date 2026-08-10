import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Venue
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services import enquiry_classification
from app.services import ivvy_import
from app.services.booking import create_booking
from app.services.contact_matching import find_or_create_contact
from app.services.lead_analytics import classify_lead_source
from app.services.notifications import notify_new_enquiry
from app.templating import templates
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


def _venue(db: Session) -> Venue:
    # Single-venue app -- same default used throughout (see
    # app/api/availability.py, app/api/admin_dashboard.py).
    return db.query(Venue).filter_by(slug="hamilton").one()


def _find_recent_duplicate(db: Session, *, contact_id, event_date, event_name) -> Booking | None:
    cutoff = dt.datetime.now(dt.timezone.utc) - DUPLICATE_SUBMISSION_WINDOW
    return db.execute(
        select(Booking)
        .where(
            Booking.contact_id == contact_id,
            Booking.event_date == event_date,
            Booking.event_name == event_name,
            Booking.created_at >= cutoff,
        )
        .order_by(Booking.created_at.desc())
    ).scalars().first()


@router.get("/enquire", response_class=HTMLResponse)
def enquiry_form(request: Request):
    return templates.TemplateResponse(request, "enquiry.html", {"event_types": EVENT_TYPES})


@router.post("/enquiries", dependencies=[Depends(rate_limit_dependency(_enquiry_rate_limiter))])
def submit_enquiry(request: Request, payload: Annotated[EnquiryCreate, Form()], db: Session = Depends(get_db)):
    venue = _venue(db)
    unassigned_space_id = ivvy_import.get_unassigned_space_id(db, venue)

    full_name = truncate(f"{payload.first_name} {payload.last_name}", 255)
    contact, duplicate_candidates = find_or_create_contact(db, full_name, payload.email, payload.phone)

    existing = _find_recent_duplicate(
        db, contact_id=contact.id, event_date=payload.event_date, event_name=payload.event_name
    )
    if existing is not None:
        return RedirectResponse(url=f"/enquiries/{existing.id}/thanks", status_code=303)

    referrer = request.headers.get("referer")
    lead_source = classify_lead_source(payload.lead_source, referrer)
    actor = truncate(f"public_enquiry:{payload.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)

    notes_parts = []
    if payload.company_name:
        notes_parts.append(f"Company: {payload.company_name}")
    if payload.dates_flexible:
        notes_parts.append("Dates flexible: yes")
    if payload.comments:
        notes_parts.append(payload.comments)
    notes = "\n".join(notes_parts) or None

    # Adult/child split: only known if the client volunteered adult_count
    # (not asked up front on this form -- see app.schemas.enquiry). Left
    # unknown, every attendee is conservatively treated as an adult for
    # minimum-spend purposes, same as before this field existed.
    if payload.adult_count is not None:
        adult_count = payload.adult_count
        child_count = payload.attendee_count - payload.adult_count
    else:
        adult_count = payload.attendee_count
        child_count = 0

    booking = create_booking(
        db,
        space_id=unassigned_space_id,
        contact_id=contact.id,
        event_date=payload.event_date,
        proposed_time_slot=payload.proposed_time_slot,
        event_name=payload.event_name,
        event_type=payload.event_type,
        adult_count=adult_count,
        child_count=child_count,
        notes=notes,
        actor=actor,
        lead_source=lead_source,
        lead_referrer=referrer,
    )

    enquiry_classification.classify_and_flag(
        db,
        booking,
        event_type=payload.event_type,
        adult_count=payload.adult_count,
        actor=actor,
        possible_duplicate_contact=len(duplicate_candidates) > 0,
    )

    # Nothing auto-sends: this only marks where a future draft pipeline
    # would pick up the enquiry, it does not send anything itself.
    notify_new_enquiry(booking)

    return RedirectResponse(url=f"/enquiries/{booking.id}/thanks", status_code=303)


@router.get("/enquiries/{booking_id}/thanks", response_class=HTMLResponse)
def enquiry_thanks(booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(request, "enquiry_submitted.html", {"booking": booking})
