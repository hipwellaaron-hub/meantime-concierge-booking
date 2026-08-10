from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.models import Space, Venue
from app.models.staff_user import StaffUser
from app.services import enquiry_classification, ivvy_import
from app.services import wizard as wizard_service
from app.templating import templates

router = APIRouter(prefix="/admin/triage", tags=["admin-triage"], dependencies=[Depends(require_staff)])


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("", response_class=HTMLResponse)
def triage(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    venue = _venue(db)
    unassigned = ivvy_import.get_unassigned_bookings(db, venue)
    wizard_ready = wizard_service.get_wizard_eligible_bookings(db, venue)
    needs_clarification = enquiry_classification.get_enquiries_needing_clarification(db, venue)
    bookable_spaces = db.scalars(
        select(Space).where(Space.venue_id == venue.id, Space.is_bookable.is_(True)).order_by(Space.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/triage.html",
        admin_ctx(
            request,
            staff,
            unassigned=unassigned,
            wizard_ready=wizard_ready,
            needs_clarification=needs_clarification,
            bookable_spaces=bookable_spaces,
        ),
    )
