import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Space, Venue
from app.schemas.availability import (
    BlockingBooking,
    SpaceAvailabilityResponse,
    SpaceCandidate,
    SpaceFreeResponse,
)
from app.services.availability import get_space_candidates, is_space_free
from app.services.validation import validate_booking_time

router = APIRouter(prefix="/availability", tags=["availability"])


@router.get("", response_model=SpaceFreeResponse)
def check_space_free(
    date: dt.date,
    space_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise HTTPException(status_code=404, detail="Space not found")

    free, blocking = is_space_free(db, space_id, date)
    return SpaceFreeResponse(
        space_id=space.id,
        space_name=space.name,
        event_date=date,
        is_free=free,
        blocking_bookings=[
            BlockingBooking(reference_code=b.reference_code, status=b.status.value, start_time=b.start_time, end_time=b.end_time)
            for b in blocking
        ],
    )


@router.get("/spaces", response_model=SpaceAvailabilityResponse)
def check_spaces_availability(
    date: dt.date,
    start: dt.time,
    end: dt.time,
    guests: int = Query(..., ge=1),
    venue_slug: str = "hamilton",
    wheelchair_accessible: bool = False,
    db: Session = Depends(get_db),
):
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")

    venue = db.query(Venue).filter_by(slug=venue_slug).one_or_none()
    if venue is None:
        raise HTTPException(status_code=404, detail=f"Unknown venue '{venue_slug}'")

    candidates = get_space_candidates(
        db,
        venue.id,
        date,
        start,
        end,
        guests,
        require_wheelchair_accessible=wheelchair_accessible,
    )
    warnings = validate_booking_time(date, start, end)

    return SpaceAvailabilityResponse(
        event_date=date,
        start_time=start,
        end_time=end,
        guest_count=guests,
        spaces=[
            SpaceCandidate(
                space_id=c["space"].id,
                space_name=c["space"].name,
                capacity=c["space"].capacity,
                min_food_spend=c["space"].min_food_spend,
                wheelchair_accessible=c["space"].wheelchair_accessible,
                is_available=c["is_available"],
                reasons=c["reasons"],
            )
            for c in candidates
        ],
        warnings=[w.message for w in warnings],
    )
