from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.venue_scope import venue_scope
from app.models import Booking, BookingEvent, Invoice, Space, Venue
from app.models.booking import BookingStatus
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.services import booking as booking_service
from app.services import documents as documents_service
from app.services import enquiry_classification, ivvy_import, wizard as wizard_service
from app.templating import templates

router = APIRouter(
    prefix="/admin/{venue_slug}", tags=["admin-dashboard"],
    dependencies=[Depends(require_staff), Depends(venue_scope)],
)


def _venue(request: Request) -> Venue:
    """The venue named in the URL, resolved by the router-level venue_scope
    dependency and stashed on request.state.

    This used to be `db.query(Venue).filter_by(slug="hamilton").one()`. Once
    the router moved onto /admin/{venue_slug}/, that made the page claim one
    venue in its URL and in its band while querying another -- which looks
    exactly like a correct page, and is worse than a visibly mixed list.

    (app/api/availability.py still carries `venue_slug: str = "hamilton"` as
    a PUBLIC query-parameter default -- a separate surface, not fixed here.)
    """
    return request.state.venue


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    venue = _venue(request)

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
    notification_failures_count = len(enquiry_classification.get_enquiry_notification_failures(db, venue))
    beos_to_review_count = len(documents_service.get_beos_awaiting_review(db, venue))
    holds_to_chase = booking_service.get_holds_to_chase(db, venue.id)

    recent_events = db.scalars(
        select(BookingEvent)
        .join(Booking, BookingEvent.booking_id == Booking.id)
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == venue.id)
        .order_by(BookingEvent.seq.desc())
        # ~10 show without scrolling; the rest are reachable in the scroll
        # window on the dashboard, so load a fuller recent history.
        .limit(60)
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
                "notification_failures": notification_failures_count,
                "beos_to_review": beos_to_review_count,
                "holds_to_chase": len(holds_to_chase),
            },
            holds_to_chase=holds_to_chase,
            recent_events=recent_events,
        ),
    )
