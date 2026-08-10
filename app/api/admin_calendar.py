import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Space, Venue
from app.models.staff_user import StaffUser
from app.services import booking as booking_service
from app.services import calendar as calendar_service
from app.templating import templates
from app.utils import truncate

router = APIRouter(prefix="/admin/calendar", tags=["admin-calendar"], dependencies=[Depends(require_staff)])

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


def _actor(staff: StaffUser) -> str:
    return truncate(f"staff:{staff.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)


@router.get("", response_class=HTMLResponse)
def calendar_week(
    request: Request,
    week: dt.date | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venue = _venue(db)
    anchor = week or dt.date.today()
    week_start = calendar_service.week_start_for(anchor)
    grid = calendar_service.get_week_grid(db, venue, week_start)
    undated_count = calendar_service.get_undated_booking_count(db, venue)

    return templates.TemplateResponse(
        request,
        "admin/calendar.html",
        admin_ctx(
            request,
            staff,
            grid=grid,
            prev_week=week_start - dt.timedelta(days=7),
            next_week=week_start + dt.timedelta(days=7),
            this_week=calendar_service.week_start_for(dt.date.today()),
            undated_count=undated_count,
        ),
    )


@router.get("/holds/new", response_class=HTMLResponse)
def new_hold_form(
    request: Request,
    date: dt.date | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venue = _venue(db)
    bookable_spaces = db.scalars(
        select(Space).where(Space.venue_id == venue.id, Space.is_bookable.is_(True)).order_by(Space.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/hold_new.html",
        admin_ctx(request, staff, bookable_spaces=bookable_spaces, default_date=date),
    )


@router.post("/holds", dependencies=[Depends(require_csrf)])
def create_hold(
    request: Request,
    space_ids: list[uuid.UUID] = Form(...),
    event_date: dt.date = Form(...),
    event_name: str = Form(...),
    start_time: str | None = Form(None),
    end_time: str | None = Form(None),
    hold_expires_at: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    try:
        parsed_start = dt.time.fromisoformat(start_time) if start_time else None
        parsed_end = dt.time.fromisoformat(end_time) if end_time else None
        parsed_expiry = dt.date.fromisoformat(hold_expires_at) if hold_expires_at else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid time or expiry date")

    created = []
    try:
        for space_id in space_ids:
            created.append(
                booking_service.create_hold(
                    db,
                    space_id=space_id,
                    event_date=event_date,
                    event_name=event_name,
                    start_time=parsed_start,
                    end_time=parsed_end,
                    hold_expires_at=parsed_expiry,
                    actor=_actor(staff),
                )
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That space is already held or booked for an overlapping time") from exc

    if len(created) == 1:
        return RedirectResponse(url=f"/admin/bookings/{created[0].id}", status_code=303)
    return RedirectResponse(
        url=f"/admin/calendar?week={calendar_service.week_start_for(event_date).isoformat()}", status_code=303
    )
