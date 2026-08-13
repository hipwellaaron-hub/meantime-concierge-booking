import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Booking, Document, Invoice, Space, Venue
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentType
from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.models.staff_user import StaffUser
from app.models.wizard_session import WizardSessionStatus
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services import booking as booking_service
from app.services import documents as documents_service
from app.services import enquiry_classification
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services import wizard_generation
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.templating import templates
from app.utils import is_valid_email, truncate

router = APIRouter(prefix="/admin/bookings", tags=["admin-bookings"], dependencies=[Depends(require_staff)])

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255

# A staff member entering a lead by hand always knows exactly how it
# reached them -- no Referer header to guess from, unlike the public
# /enquire form (app.services.lead_analytics.classify_lead_source).
STAFF_LEAD_SOURCES: tuple[str, ...] = ("phone", "direct_email", "ivvy_marketplace", "own_website", "other")


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
    status: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # The "Any" option in the status filter submits status="" -- FastAPI's
    # Optional[BookingStatus] does not coerce an empty string to None the
    # way Optional[str] does, so this was parsed as a plain str and
    # converted by hand instead of declared as BookingStatus directly.
    try:
        parsed_status = BookingStatus(status) if status else None
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")

    venue = _venue(db)
    bookings = booking_service.search_bookings(db, venue.id, status=parsed_status, query=q)
    return templates.TemplateResponse(
        request,
        "admin/bookings_list.html",
        admin_ctx(request, staff, bookings=bookings, status=parsed_status, q=q or "", statuses=list(BookingStatus)),
    )


@router.get("/new", response_class=HTMLResponse)
def new_booking_form(request: Request, staff: StaffUser = Depends(require_staff)):
    return templates.TemplateResponse(
        request,
        "admin/booking_new.html",
        admin_ctx(request, staff, event_types=EVENT_TYPES, lead_sources=STAFF_LEAD_SOURCES),
    )


@router.post("/new", dependencies=[Depends(require_csrf)])
def create_new_booking(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    phone: str | None = Form(None),
    company_name: str | None = Form(None),
    event_name: str = Form(...),
    event_date: str | None = Form(None),
    dates_flexible: bool = Form(...),
    event_type: str = Form(...),
    attendee_count: str | None = Form(None),
    adult_count: str | None = Form(None),
    proposed_time_slot: str | None = Form(None),
    comments: str | None = Form(None),
    lead_source: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # require_csrf (this route's own dependency) already binds its own
    # scalar `csrf_token: str = Form(...)` field -- FastAPI can't also bind
    # a full Pydantic model via Form() in the same request alongside that,
    # so the fields are collected individually here (matching every other
    # route in this file) and handed to EnquiryCreate for the same
    # validation the public /enquiries form gets, rather than duplicating
    # it by hand.
    try:
        payload = EnquiryCreate(
            first_name=first_name, last_name=last_name, email=email, phone=phone,
            company_name=company_name, event_name=event_name, event_date=event_date,
            dates_flexible=dates_flexible, event_type=event_type, attendee_count=attendee_count,
            adult_count=adult_count, proposed_time_slot=proposed_time_slot, comments=comments,
            lead_source=lead_source,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    if payload.lead_source not in STAFF_LEAD_SOURCES:
        raise HTTPException(status_code=422, detail="Choose how this lead reached you")

    venue = _venue(db)
    full_name = truncate(f"{payload.first_name} {payload.last_name}", 255)
    booking, _duplicate_candidates, _is_new = enquiry_classification.create_enquiry_booking(
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
        lead_source=payload.lead_source,
        lead_referrer=None,
        actor=_actor(staff),
    )
    return _redirect_to_detail(booking.id)


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
            legal_next_statuses=booking_service.LEGAL_TRANSITIONS.get(booking.status, ()),
            contact_email_valid=booking.contact is not None and is_valid_email(booking.contact.email),
        ),
    )


@router.post("/{booking_id}/status", dependencies=[Depends(require_csrf)])
def transition_booking_status(
    booking_id: uuid.UUID,
    request: Request,
    new_status: BookingStatus = Form(...),
    reason: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        booking_service.transition_status(
            db, booking, new_status, actor=_actor(staff), reason=reason or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/hold-expiry", dependencies=[Depends(require_csrf)])
def set_hold_expiry(
    booking_id: uuid.UUID,
    request: Request,
    hold_expires_at: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        parsed = dt.date.fromisoformat(hold_expires_at) if hold_expires_at else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid expiry date")
    booking_service.set_hold_expiry(db, booking, hold_expires_at=parsed, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/assign-space", dependencies=[Depends(require_csrf)])
def assign_space(
    booking_id: uuid.UUID,
    request: Request,
    space_id: uuid.UUID = Form(...),
    start_time: dt.time = Form(...),
    end_time: dt.time = Form(...),
    event_date: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        parsed_event_date = dt.date.fromisoformat(event_date) if event_date else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid event date")
    try:
        booking_service.assign_space_and_time(
            db,
            booking,
            space_id=space_id,
            start_time=start_time,
            end_time=end_time,
            event_date=parsed_event_date,
            actor=_actor(staff),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That space is already booked for an overlapping time") from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/linked-spaces", dependencies=[Depends(require_csrf)])
def add_linked_space(
    booking_id: uuid.UUID,
    request: Request,
    space_id: uuid.UUID = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        booking_service.add_linked_space(db, booking, space_id=space_id, actor=_actor(staff))
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
    if doc_type == DocumentType.agreement:
        content = generate_agreement_content(booking)
    else:
        session = booking.wizard_session
        if session is not None and session.status == WizardSessionStatus.submitted:
            # A completed wizard already has the client's real food/
            # beverage/music/extras answers -- generating blind [REVIEW]
            # placeholders instead would silently throw that away just
            # because staff triggered this by hand rather than the client
            # submitting (see app.services.wizard_generation).
            content = wizard_generation.build_beo_content_for_session(db, session)
        else:
            content = generate_beo_content(booking)
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


@router.get("/{booking_id}/documents/{document_id}/preview", response_class=HTMLResponse)
def preview_document(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The public /d/{token} view refuses a draft outright (never human-
    approved for client eyes) -- this is the staff-only equivalent so
    someone can actually read a generated BEO/agreement's content, not
    just Send/Regenerate/Delete it blind. Reuses the exact template a
    client would see; does not call record_view, since a staff read must
    never be mistaken for the client having seen it."""
    _get_booking_or_404(db, booking_id)
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    return templates.TemplateResponse(
        request, "document.html", {"document": document, "booking": document.booking, "is_staff_preview": True}
    )


@router.post("/{booking_id}/documents/{document_id}/delete", dependencies=[Depends(require_csrf)])
def delete_document(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    try:
        documents_service.delete_draft(db, document, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/invoices/{invoice_id}/delete", dependencies=[Depends(require_csrf)])
def delete_invoice(
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
        invoicing.delete_draft(db, invoice, actor=_actor(staff))
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


@router.post("/{booking_id}/invoices/final", dependencies=[Depends(require_csrf)])
def create_final_invoice(
    booking_id: uuid.UUID,
    request: Request,
    due_date: dt.date = Form(...),
    description: list[str] = Form(...),
    quantity: list[str] = Form(...),
    unit_price: list[str] = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)

    line_items = []
    for desc, qty, price in zip(description, quantity, unit_price):
        desc = desc.strip()
        if not desc:
            continue  # a blank row in the form -- not a real line item
        try:
            parsed_qty = Decimal(qty)
            parsed_price = Decimal(price)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail=f"Invalid quantity or unit price for line item '{desc}'")
        line_items.append({"description": desc, "quantity": str(parsed_qty), "unit_price": str(parsed_price)})

    if not line_items:
        raise HTTPException(status_code=422, detail="At least one line item is required")

    try:
        invoicing.create_final_invoice(db, booking, line_items=line_items, due_date=due_date, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.get("/{booking_id}/invoices/{invoice_id}/preview", response_class=HTMLResponse)
def preview_invoice(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Staff-only equivalent of /i/{token} for a draft invoice, same
    reasoning as preview_document above. No live Stripe card-payment-link
    call here -- irrelevant for a draft nobody can pay yet, and a wasted
    API call on every preview."""
    _get_booking_or_404(db, booking_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Invoice not found on this booking")
    summary = invoicing.get_payment_summary(db, invoice)
    other_invoices = [
        inv for inv in invoice.booking.invoices
        if inv.id != invoice.id and inv.status != InvoiceStatus.draft
    ]
    return templates.TemplateResponse(
        request,
        "invoice.html",
        {
            "invoice": invoice,
            "booking": invoice.booking,
            "summary": summary,
            "gst_component": invoicing.gst_component(invoice.total),
            "line_items": invoicing.line_item_breakdown(invoice.line_items),
            "other_invoices": other_invoices,
            "stripe_configured": False,
            "card_payment_url": None,
            "card_payment_amount": None,
            "is_staff_preview": True,
        },
    )


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
    try:
        wizard_service.get_or_create_session(db, booking, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
