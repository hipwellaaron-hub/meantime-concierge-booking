import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Booking, Document, Invoice, Space, Venue
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentType
from app.models.payment import PaymentMethod
from app.models.staff_user import StaffUser
from app.services import booking as booking_service
from app.services import documents as documents_service
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.templating import templates
from app.utils import truncate

router = APIRouter(prefix="/admin/bookings", tags=["admin-bookings"], dependencies=[Depends(require_staff)])

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _actor(staff: StaffUser) -> str:
    return truncate(f"staff:{staff.email}", BOOKING_EVENT_ACTOR_MAX_LENGTH)


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


def _get_booking_or_404(db: Session, booking_id: uuid.UUID) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


def _redirect_to_detail(booking_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(url=f"/admin/bookings/{booking_id}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_bookings(
    request: Request,
    status: BookingStatus | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venue = _venue(db)
    bookings = booking_service.search_bookings(db, venue.id, status=status, query=q)
    return templates.TemplateResponse(
        request,
        "admin/bookings_list.html",
        admin_ctx(request, staff, bookings=bookings, status=status, q=q or "", statuses=list(BookingStatus)),
    )


@router.get("/{booking_id}", response_class=HTMLResponse)
def booking_detail(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    booking = _get_booking_or_404(db, booking_id)
    bookable_spaces = db.scalars(
        select(Space).where(Space.venue_id == booking.space.venue_id, Space.is_bookable.is_(True)).order_by(Space.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/booking_detail.html",
        admin_ctx(
            request,
            staff,
            booking=booking,
            bookable_spaces=bookable_spaces,
            min_reduction_reasons=list(MinReductionReasonCode),
            payment_methods=list(PaymentMethod),
        ),
    )


@router.post("/{booking_id}/assign-space", dependencies=[Depends(require_csrf)])
def assign_space(
    booking_id: uuid.UUID,
    request: Request,
    space_id: uuid.UUID = Form(...),
    start_time: dt.time = Form(...),
    end_time: dt.time = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        booking_service.assign_space_and_time(
            db, booking, space_id=space_id, start_time=start_time, end_time=end_time, actor=_actor(staff)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That space is already booked for an overlapping time") from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/documents/{doc_type}/generate", dependencies=[Depends(require_csrf)])
def generate_document(
    booking_id: uuid.UUID,
    doc_type: DocumentType,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    content = generate_agreement_content(booking) if doc_type == DocumentType.agreement else generate_beo_content(booking)
    documents_service.create_new_version(db, booking, doc_type, content, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/documents/{document_id}/send", dependencies=[Depends(require_csrf)])
def send_document(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    # A direct lookup, not booking.documents -- the relationship can be
    # stale within a session that outlives a single request (as every
    # admin test's shared `db` fixture does; a real per-request session
    # never has this problem, but a direct query is correct either way).
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    try:
        documents_service.mark_sent(db, document, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/invoices/deposit", dependencies=[Depends(require_csrf)])
def create_deposit_invoice(
    booking_id: uuid.UUID,
    request: Request,
    due_date: dt.date = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    invoicing.create_deposit_invoice(db, booking, due_date=due_date, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/invoices/{invoice_id}/send", dependencies=[Depends(require_csrf)])
def send_invoice(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Invoice not found on this booking")
    try:
        invoicing.mark_sent(db, invoice, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/invoices/{invoice_id}/payments", dependencies=[Depends(require_csrf)])
def record_payment(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    amount: str = Form(...),
    method: PaymentMethod = Form(...),
    reference: str | None = Form(None),
    payer_name: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Invoice not found on this booking")
    try:
        parsed_amount = Decimal(amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="Invalid payment amount")
    try:
        invoicing.record_payment(
            db,
            invoice,
            amount=parsed_amount,
            method=method,
            reference=reference or None,
            payer_name=payer_name or None,
            actor=_actor(staff),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/wizard/send", dependencies=[Depends(require_csrf)])
def send_wizard_link(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    booking = _get_booking_or_404(db, booking_id)
    wizard_service.get_or_create_session(db, booking, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/wizard/revoke", dependencies=[Depends(require_csrf)])
def revoke_wizard_link(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    booking = _get_booking_or_404(db, booking_id)
    if booking.wizard_session is None:
        raise HTTPException(status_code=404, detail="No wizard session exists for this booking")
    try:
        wizard_service.revoke_session(db, booking.wizard_session, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/policy/setup-access/confirm", dependencies=[Depends(require_csrf)])
def confirm_setup_access(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        booking_service.confirm_setup_access(db, booking, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/policy/agreed-minimum", dependencies=[Depends(require_csrf)])
def set_agreed_minimum(
    booking_id: uuid.UUID,
    request: Request,
    agreed_min_adults: int = Form(...),
    reason: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        parsed_reason = MinReductionReasonCode(reason) if reason else None
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown reason code {reason}")
    try:
        booking_service.set_agreed_minimum(
            db, booking, agreed_min_adults=agreed_min_adults, reason=parsed_reason, actor=_actor(staff)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/policy/outside-cake", dependencies=[Depends(require_csrf)])
def set_outside_cake_permitted(
    booking_id: uuid.UUID,
    request: Request,
    permitted: bool = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    booking_service.set_outside_cake_permitted(db, booking, permitted=permitted, actor=_actor(staff))
    return _redirect_to_detail(booking_id)
