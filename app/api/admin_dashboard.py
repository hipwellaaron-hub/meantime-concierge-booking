from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.models import Booking, BookingEvent, Invoice, Space, Venue
from app.models.booking import BookingStatus
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.services import ivvy_import, wizard as wizard_service
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin-dashboard"], dependencies=[Depends(require_staff)])


def _venue(db: Session) -> Venue:
    # Single-venue app -- same default used by app/api/availability.py.
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    venue = _venue(db)

    open_enquiries = db.scalar(
        select(func.count(Booking.id))
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == venue.id, Booking.status == BookingStatus.enquiry)
    )
    triage_count = len(ivvy_import.get_unassigned_bookings(db, venue))
    wizard_ready_count = len(wizard_service.get_wizard_eligible_bookings(db, venue))
    unpaid_invoices = db.scalar(
        select(func.count(Invoice.id))
        .join(Booking, Invoice.booking_id == Booking.id)
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == venue.id, Invoice.status == InvoiceStatus.sent)
    )

    recent_events = db.scalars(
        select(BookingEvent)
        .join(Booking, BookingEvent.booking_id == Booking.id)
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == venue.id)
        .order_by(BookingEvent.created_at.desc())
        .limit(15)
    ).all()

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        admin_ctx(
            request,
            staff,
            counts={
                "open_enquiries": open_enquiries,
                "triage": triage_count,
                "wizard_ready": wizard_ready_count,
                "unpaid_invoices": unpaid_invoices,
            },
            recent_events=recent_events,
        ),
    )
