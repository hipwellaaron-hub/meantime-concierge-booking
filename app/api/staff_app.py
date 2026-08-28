"""The Meantime Floor app's read-only API. Everything here answers one
operational question for the floor and bar team -- what's on, when, and
has it paid -- from data Concierge already holds. Nothing is created or
edited through these routes, ever.

Auth is a DB-backed bearer token (see app.models.staff_app_token),
revocable per-device from the admin staff page. Both roles may use the
app; only role='floor' is BLOCKED from /admin (see app.admin_auth).
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Document, Invoice, Space, Venue
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.services import documents as documents_service
from app.services import staff_auth
from app.services.pdf import render_html_to_pdf
from app.templating import templates

router = APIRouter(prefix="/api/staff", tags=["staff-app"])

# Same shape as the admin login limiter: brute-force protection on a
# credentialed endpoint, generous enough for a small team's real logins.
_app_login_rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=300)

# The two "locked" booking states the app shows -- per Aaron's explicit
# decision, tentative holds are NOT shown (a hold on Saturday reads as a
# free night to the floor team, and that is the chosen trade-off).
FLOOR_VISIBLE_STATUSES = (BookingStatus.confirmed, BookingStatus.completed)


class StaffLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=255)


def require_app_token(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> StaffUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    staff = staff_auth.get_staff_by_app_token(db, authorization.removeprefix("Bearer ").strip())
    if staff is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return staff


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.post("/login", dependencies=[Depends(rate_limit_dependency(_app_login_rate_limiter))])
def app_login(payload: StaffLogin, request: Request, db: Session = Depends(get_db)):
    staff = staff_auth.authenticate(db, payload.email, payload.password)
    if staff is None:
        raise HTTPException(status_code=401, detail="Wrong email or password")
    token = staff_auth.issue_app_token(db, staff)
    return {"token": token, "name": staff.name}


def _payment_status(db: Session, booking: Booking) -> str:
    """"paid" only when at least one invoice is paid AND nothing
    non-cancelled is still unpaid (draft or sent). A confirmed function
    with no invoices at all is "outstanding" -- the weekend question is
    "did they pay?", and the honest answer there is no. Status only:
    no amounts ever cross this API."""
    invoices = db.scalars(
        select(Invoice).where(Invoice.booking_id == booking.id, Invoice.status != InvoiceStatus.cancelled)
    ).all()
    has_paid = any(i.status == InvoiceStatus.paid for i in invoices)
    has_unpaid = any(i.status in (InvoiceStatus.draft, InvoiceStatus.sent) for i in invoices)
    return "paid" if has_paid and not has_unpaid else "outstanding"


def _beo_ready(db: Session, booking: Booking) -> bool:
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    return current is not None and current.status != DocumentStatus.draft


def _booking_payload(db: Session, booking: Booking) -> dict:
    spaces = [booking.space.name]
    for child in booking.linked_bookings:
        if child.status in FLOOR_VISIBLE_STATUSES:
            spaces.append(child.space.name)
    return {
        "id": str(booking.id),
        "date": booking.event_date.isoformat() if booking.event_date else None,
        "space": booking.space.name,
        "spaces": spaces,
        "event_name": booking.event_name,
        "event_type": booking.event_type,
        "start_time": booking.start_time.strftime("%H:%M") if booking.start_time else None,
        "end_time": booking.end_time.strftime("%H:%M") if booking.end_time else None,
        "adults": booking.adult_count,
        "kids": booking.child_count,
        "status": booking.status.value,
        "beo_ready": _beo_ready(db, booking),
        "payment_status": _payment_status(db, booking),
    }


@router.get("/bookings")
def list_bookings(
    request: Request,
    from_date: dt.date | None = Query(default=None, alias="from"),
    to_date: dt.date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    venue = _venue(db)
    query = (
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == venue.id,
            Booking.status.in_(FLOOR_VISIBLE_STATUSES),
            # Linked children are their parent's second room, not a
            # separate function -- the parent's payload carries both
            # spaces instead.
            Booking.parent_booking_id.is_(None),
            Booking.event_date.isnot(None),
        )
        .order_by(Booking.event_date, Booking.start_time)
    )
    if from_date is not None:
        query = query.where(Booking.event_date >= from_date)
    if to_date is not None:
        query = query.where(Booking.event_date <= to_date)
    return {"bookings": [_booking_payload(db, b) for b in db.scalars(query).all()]}


def _get_visible_booking_or_404(db: Session, booking_id: uuid.UUID) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None or booking.status not in FLOOR_VISIBLE_STATUSES or booking.parent_booking_id is not None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.get("/bookings/{booking_id}")
def booking_detail(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    booking = _get_visible_booking_or_404(db, booking_id)
    payload = _booking_payload(db, booking)
    # The floor team's run-order facts. Read-only; no client contact
    # details, no dollar figures.
    payload["setup_access_time"] = booking.setup_access_time.strftime("%H:%M") if booking.setup_access_time else None
    payload["setup_access_confirmed"] = booking.setup_access_confirmed
    payload["food_service_time"] = booking.food_service_time.strftime("%H:%M") if booking.food_service_time else None
    payload["guest_arrival_time"] = booking.guest_arrival_time.strftime("%H:%M") if booking.guest_arrival_time else None
    return payload


def _get_finalised_beo_or_404(db: Session, booking: Booking) -> Document:
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    if current is None or current.status == DocumentStatus.draft:
        raise HTTPException(status_code=404, detail="No finalised BEO for this booking")
    return current


@router.get("/bookings/{booking_id}/beo", response_class=HTMLResponse)
def booking_beo(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    """The finalised BEO, rendered for the floor team. Deliberately NOT
    the public /d/{token} link: the first open of that link marks the
    document viewed, and "viewed" must keep meaning THE CLIENT saw it.
    is_floor_app shows the internal kitchen/bar notes -- this is exactly
    the staff surface they exist for."""
    booking = _get_visible_booking_or_404(db, booking_id)
    document = _get_finalised_beo_or_404(db, booking)
    return templates.TemplateResponse(
        request, "document.html", {"document": document, "booking": booking, "is_floor_app": True}
    )


@router.get("/bookings/{booking_id}/beo.pdf")
def booking_beo_pdf(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    """Download/share copy: the CLEAN client render, without internal
    notes -- a shared PDF can leave the team."""
    booking = _get_visible_booking_or_404(db, booking_id)
    document = _get_finalised_beo_or_404(db, booking)
    html = templates.get_template("document.html").render(document=document, booking=booking, is_pdf=True)
    filename = f"{booking.reference_code}-BEO-v{document.version}.pdf"
    return Response(
        content=render_html_to_pdf(html),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
