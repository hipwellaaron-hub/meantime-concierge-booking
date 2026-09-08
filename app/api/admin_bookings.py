import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import (
    BeoProposal,
    BeoProposalField,
    Booking,
    BookingEvent,
    BookingVendor,
    Document,
    Invoice,
    Space,
    Venue,
)
from app.models.booking_vendor import VendorType
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.models.staff_user import StaffUser
from app.models.wizard_session import WizardSessionStatus
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services import booking as booking_service
from app.services import policy
from app.services.pdf import render_html_to_pdf
from app.services.contact_matching import (
    find_contact_by_email,
    find_or_create_contact,
    update_contact_details,
)
from app.services import beo_proposals as beo_proposals_service
from app.services import content_authorship
from app.services import document_regeneration
from app.services import documents as documents_service
from app.services import enquiry_classification
from app.services import invoicing
from app.services import legacy_documents
from app.services import wizard as wizard_service
from app.services import wizard_generation
from app.services.attribution import summarize_channel
from app.services.document_generation import (
    NO_DIETARIES,
    REVIEW,
    build_event_timeline,
    build_total_food_spend,
    build_vendor_snapshot,
    compute_food_order_total,
    generate_agreement_content,
    generate_beo_content,
    rebuild_terms_text,
)
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


@router.post("/{booking_id}/delete", dependencies=[Depends(require_csrf)])
def delete_booking(
    booking_id: uuid.UUID,
    request: Request,
    confirm_reference: str = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Hard-delete a booking and everything attached to it. The only hard
    delete in the app -- guarded by requiring the exact reference code to
    be typed, so it can't happen on a stray click. For removing test or
    erroneous bookings; recoverable only via database PITR."""
    booking = _get_booking_or_404(db, booking_id)
    if confirm_reference.strip() != booking.reference_code:
        raise HTTPException(
            status_code=422,
            detail="Type the booking's exact reference code to confirm deletion",
        )
    try:
        booking_service.delete_booking_and_dependents(db, booking, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/bookings", status_code=303)


# --- legacy document uploads (iVvy migration) --------------------------------


def _parse_legacy_date(value: str | None) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date (use YYYY-MM-DD)") from exc
    return dt.datetime.combine(d, dt.time(12, 0), tzinfo=dt.timezone.utc)


def _read_upload(file: UploadFile) -> bytes:
    # Bound the read at the cap + 1 byte so an over-size upload can't be
    # slurped whole into memory; validate_pdf then rejects it cleanly.
    return file.file.read(legacy_documents.MAX_PDF_BYTES + 1)


@router.post("/{booking_id}/legacy-agreement", dependencies=[Depends(require_csrf)])
def upload_legacy_agreement(
    booking_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    source_ref: str | None = Form(None),
    signed_date: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        legacy_documents.attach_agreement_pdf(
            db, booking, pdf=_read_upload(file), filename=truncate(file.filename or "agreement.pdf", 255),
            source_ref=(source_ref or "").strip() or None, signed_at=_parse_legacy_date(signed_date),
            actor=_actor(staff),
        )
    except legacy_documents.LegacyUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/legacy-deposit", dependencies=[Depends(require_csrf)])
def upload_legacy_deposit(
    booking_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    amount: str = Form(...),
    source_ref: str | None = Form(None),
    paid_date: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        parsed_amount = Decimal(amount)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="Invalid deposit amount") from exc
    if parsed_amount <= 0:
        raise HTTPException(status_code=422, detail="Deposit amount must be positive")
    try:
        legacy_documents.attach_deposit_pdf(
            db, booking, pdf=_read_upload(file), filename=truncate(file.filename or "deposit.pdf", 255),
            amount=parsed_amount, paid_at=_parse_legacy_date(paid_date),
            source_ref=(source_ref or "").strip() or None, actor=_actor(staff),
        )
    except legacy_documents.LegacyUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


def _legacy_file_response(filename: str | None, data: bytes) -> Response:
    # Opaque bytes, forced download, never sniffed into an inline render.
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename or "legacy.pdf"}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{booking_id}/documents/{document_id}/legacy-file")
def serve_legacy_agreement_file(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    doc = db.get(Document, document_id)
    if doc is None or doc.booking_id != booking.id or not doc.is_legacy or doc.legacy_file is None:
        raise HTTPException(status_code=404, detail="No legacy file on this document")
    return _legacy_file_response(doc.legacy_filename, doc.legacy_file)


@router.get("/{booking_id}/invoices/{invoice_id}/legacy-file")
def serve_legacy_deposit_file(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    inv = db.get(Invoice, invoice_id)
    if inv is None or inv.booking_id != booking.id or not inv.is_legacy or inv.legacy_file is None:
        raise HTTPException(status_code=404, detail="No legacy file on this invoice")
    return _legacy_file_response(inv.legacy_filename, inv.legacy_file)


@router.get("", response_class=HTMLResponse)
def list_bookings(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # The default option submits status="" -- FastAPI's Optional[BookingStatus]
    # does not coerce an empty string to None the way Optional[str] does, so
    # this is parsed as a plain str and converted by hand. status="all" is a
    # deliberate "show everything including terminal" escape hatch; the empty
    # default shows the live pipeline only (see search_bookings).
    include_terminal = status == "all"
    try:
        parsed_status = None if (not status or status == "all") else BookingStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")

    venue = _venue(db)
    bookings = booking_service.search_bookings(
        db, venue.id, status=parsed_status, query=q, include_terminal=include_terminal
    )
    return templates.TemplateResponse(
        request,
        "admin/bookings_list.html",
        admin_ctx(
            request,
            staff,
            bookings=bookings,
            status=parsed_status,
            status_filter=status or "",
            q=q or "",
            statuses=list(BookingStatus),
        ),
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
    beo_draft = beo_proposals_service.current_draft_beo(db, booking_id)
    beo_review_rows = beo_proposals_service.review_rows(db, booking_id, document=beo_draft)
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
            enquiry_notification_failed=any(
                e.event_type == "enquiry_notification_failed" for e in booking.events
            ) and booking.enquiry_notification_sent_at is None,
            beo_proposal_waiting=len(beo_review_rows),
            beo_proposal_document_id=beo_draft.id if beo_draft is not None else None,
            first_touch_channel=summarize_channel(booking.first_touch_attribution),
            last_touch_channel=summarize_channel(booking.last_touch_attribution),
            touches_differ=booking.first_touch_attribution != booking.last_touch_attribution,
            conversion_dispatches=list(booking.conversion_dispatches),
            legacy_mismatches=legacy_documents.legacy_mismatches(booking),
            # Which document types are mid-revision: a draft is current and
            # an earlier version of the same type has already been sent, so
            # the client is holding a link that 410s until this one goes
            # out. Aaron: "I'd rather see at a glance that a client
            # currently has no link."
            # The audit table's "Old" column means the previous value on
            # every row but these, where it holds the names of the fields a
            # save changed. Named here so the reader and the writer cannot
            # drift apart.
            field_list_events=documents_service.FIELD_LIST_IN_OLD_VALUE,
            awaiting_resend={
                doc_type.value
                for doc_type in DocumentType
                if documents_service.is_mid_revision(db, booking.id, doc_type)
            },
            # Prefilled into the "create final invoice" form so the hand
            # path and the wizard path date an invoice the same way. Staff
            # can still type over it -- the route takes whatever is posted.
            suggested_final_due_date=policy.final_balance_due_date(
                booking.event_date, issued_on=dt.date.today()
            ),
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


@router.post("/{booking_id}/status/unpin", dependencies=[Depends(require_csrf)])
def hand_status_back_to_automation(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Clears the manual-override pin a hand-set status leaves behind, so
    the automatic transitions may act on this booking again (and catch up
    immediately). See Booking.status_pinned_at."""
    booking = _get_booking_or_404(db, booking_id)
    booking_service.clear_status_pin(db, booking, actor=_actor(staff))
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


@router.post("/{booking_id}/contact", dependencies=[Depends(require_csrf)])
def set_booking_contact(
    booking_id: uuid.UUID,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str | None = Form(None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Attaches a contact to a booking, or corrects the one it has.

    The form serves two different intentions and they need different
    handling, which is what went wrong before: sending everything through
    find_or_create_contact meant a correction to an existing contact was
    silently dropped, because that function returns an email match
    untouched (deliberately -- see update_contact_details).

    - Correcting the booking's own contact (email unchanged, or changed to
      an address nobody else uses) updates that record IN PLACE and audits
      each changed field. Fixing a doubled name or a typo'd address must
      fix the person, not fork a second row and leave the old one behind.
    - An email belonging to somebody else means "this booking is actually
      theirs": the booking is repointed to them and their stored details
      are left alone, so one booking's form can never rewrite another
      person's record.
    """
    booking = _get_booking_or_404(db, booking_id)
    name, email = name.strip(), email.strip()
    phone = (phone or "").strip() or None
    if not name or not email:
        raise HTTPException(status_code=422, detail="Name and email are both required")
    # Length-checked here rather than letting the database raise: these
    # come from a form with no maxlength on two of the three inputs.
    if len(name) > 255 or len(email) > 320 or (phone and len(phone) > 50):
        raise HTTPException(
            status_code=422,
            detail="Name (255), email (320) or phone (50) is longer than the field allows",
        )

    owner_of_email = find_contact_by_email(db, email)
    current = booking.contact

    if current is not None and (owner_of_email is None or owner_of_email.id == current.id):
        update_contact_details(
            db, current, name=name, email=email, phone=phone,
            actor=_actor(staff), booking_id=booking.id,
        )
    else:
        contact, _duplicates = find_or_create_contact(db, name, email, phone)
        booking_service.set_contact(db, booking, contact_id=contact.id, actor=_actor(staff))
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


def _fresh_document_content(db: Session, booking: Booking, doc_type: DocumentType) -> dict:
    if doc_type == DocumentType.agreement:
        return generate_agreement_content(booking)
    session = booking.wizard_session
    if session is not None and session.status == WizardSessionStatus.submitted:
        # A completed wizard already has the client's real food/
        # beverage/music/extras answers -- generating blind [REVIEW]
        # placeholders instead would silently throw that away just
        # because staff triggered this by hand rather than the client
        # submitting (see app.services.wizard_generation).
        return wizard_generation.build_beo_content_for_session(db, session)
    return generate_beo_content(booking)


@router.post("/{booking_id}/documents/{doc_type}/generate", dependencies=[Depends(require_csrf)])
def generate_document(
    booking_id: uuid.UUID,
    doc_type: DocumentType,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Regenerating rebuilds the document from the booking. Where that
    would destroy something a person wrote -- an approved allergy note, a
    hand-edited instruction -- it stops and asks, naming every field.
    Silent loss of a declared allergy is the thing this whole feature
    exists to prevent (Aaron, 2026-09-06), and a regenerate was doing it.
    Nothing else changes: with nothing to lose, this is the same one click
    it has always been."""
    booking = _get_booking_or_404(db, booking_id)
    content = _fresh_document_content(db, booking, doc_type)
    # Locked, not merely read: this path decides "nothing is at risk" and
    # then WRITES on that decision, so it needs the same window closed as
    # the confirm path below. An approval landing between the two used to
    # be destroyed with no confirmation shown and the approver told it had
    # succeeded (proved live, 2026-09-06 re-review).
    current = documents_service.lock_current_for_update(db, booking.id, doc_type)
    losses = document_regeneration.losses(db, current, content)
    pending = _pending_proposal_rows(db, booking, doc_type, current)
    # Pending work counts even with nothing to lose: otherwise the one case
    # that shows no screen at all is the one that silently invalidates it.
    if losses or pending:
        return _render_regenerate_confirmation(request, db, booking, doc_type, current, losses, staff, pending)
    documents_service.create_new_version(db, booking, doc_type, content, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


def _pending_proposal_rows(db, booking, doc_type, current) -> list[dict]:
    """A pending proposal is reviewed against a specific version. Creating a
    new one leaves it needing re-approval, and approving it afterwards now
    fails with "replaced by a newer version" (beo_proposals._locked_draft).
    Aaron: "If a regenerate silently invalidates pending work, I will hit
    exactly that error without knowing why. Tell me before, not after."
    """
    if doc_type != DocumentType.beo or current is None:
        return []
    return beo_proposals_service.review_rows(db, booking.id, document=current)


def _render_regenerate_confirmation(request, db, booking, doc_type, current, losses, staff, pending):
    return templates.TemplateResponse(
        request,
        "admin/regenerate_confirm.html",
        {
            **admin_ctx(request, staff),
            "booking": booking,
            "doc_type": doc_type,
            "document": current,
            "losses": losses,
            "pending_proposal_rows": pending,
            "expect": document_regeneration.fingerprint(losses, pending),
            "hand_edit": document_regeneration.was_hand_edited(db, current),
        },
        status_code=409,
    )


@router.post("/{booking_id}/documents/{doc_type}/generate/confirm", dependencies=[Depends(require_csrf)])
def generate_document_confirmed(
    booking_id: uuid.UUID,
    doc_type: DocumentType,
    request: Request,
    expect: str = Form(...),
    keep: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The decision, applied. Values for the kept fields are read from the
    document at write time, never from the form -- a value that travelled
    through a browser and back is not the one that was approved.

    `expect` is a compare-and-set on the exact set of losses that was
    shown. If another approval landed, or the booking changed, in the
    seconds in between, the human was answering a question about different
    values: re-ask rather than write."""
    booking = _get_booking_or_404(db, booking_id)
    content = _fresh_document_content(db, booking, doc_type)
    # Locked before the losses are read and held until the version is
    # written: an approval landing in that window used to be silently
    # reverted, with the audit line still claiming the value was kept
    # (proved live with two sessions, 2026-09-06 review).
    current = documents_service.lock_current_for_update(db, booking.id, doc_type)
    losses = document_regeneration.losses(db, current, content)
    # Read here, above the straight-through write, and not only inside the
    # refusal below. A screen shown because of pending work alone reached
    # a confirm with no losses -- which returned before the compare-and-set
    # ran at all, so the one screen whose entire purpose is "tell me
    # before, not after" was the one screen whose answer was never checked.
    pending = _pending_proposal_rows(db, booking, doc_type, current)
    if not losses and not pending:
        # Nothing at risk and nothing outstanding: the ordinary one click,
        # with no question asked and none to check.
        documents_service.create_new_version(db, booking, doc_type, content, actor=_actor(staff))
        return _redirect_to_detail(booking_id)
    if document_regeneration.fingerprint(losses, pending) != expect:
        # Nothing is written; the lock is released when the request ends
        # and get_db closes the session.
        return _render_regenerate_confirmation(request, db, booking, doc_type, current, losses, staff, pending)

    # Only the fields this person was actually ASKED about. Being a
    # protected name is not the same question: the form offers a checkbox
    # per loss, so any other name arriving here was never on the screen --
    # and `regenerated_note` is built from the losses, so a keep outside
    # that set is written with nothing in the audit trail able to mention
    # it. Proved: a POST keeping `terms_sections` on a BEO wrote
    # terms_sections and terms_text into it, and froze an unoffered field
    # over the [REVIEW] prompt that would have asked someone to fill it
    # in, under the note "kept Room layout notes".
    #
    # `losses` is the local the fingerprint was just checked against, not
    # a fresh losses() call -- recomputing would reopen the check-to-write
    # window the row lock exists to close.
    offered = {loss.field for loss in losses}
    keep_fields = {name for name in keep if name in offered}
    merged = document_regeneration.apply_choices(content, current, keep_fields)
    documents_service.create_new_version(
        db, booking, doc_type, merged, actor=_actor(staff),
        # With no losses there was no keep decision, so there is none to
        # record -- the screen was shown for the pending work alone, and
        # summarise() would only say "no human values affected".
        regenerated_note=document_regeneration.summarise(losses, keep_fields) if losses else None,
    )
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/documents/{document_id}/revise", dependencies=[Depends(require_csrf)])
def revise_document(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Reopen a sent document for editing by copying it forward.

    The alternative was Regenerate, which rebuilds from the booking and
    destroys hand-entered content -- the reason every other guard in this
    module exists. Nothing is rebuilt here, so nothing can be lost.

    Redirects to the new draft's edit form rather than the booking page:
    somebody clicking Revise is mid-sentence, not browsing.
    """
    booking = _get_booking_or_404(db, booking_id)
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    try:
        draft = documents_service.revise(db, document, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/admin/bookings/{booking.id}/documents/{draft.id}/edit", status_code=303
    )


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


@router.get("/{booking_id}/documents/{document_id}/pdf")
def download_document_pdf_for_staff(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The INTERNAL copy of the document, as a PDF.

    The client PDF at /d/{token}/pdf is the clean render, and until now it
    was the only one -- the admin's own "Download PDF" link pointed at it.
    So the file staff download and read on a phone in the venue was the one
    designed to hide staff-facing detail: a [REVIEW] marker that shows on
    the web preview becomes "To be confirmed - contact the venue" there.
    That is how a page of missing content went unnoticed on
    HAM-20260911-AKPSO (Aaron, 2026-09-08: "the PDF is what I download and
    what gets read on a phone in the venue, and that's where I missed it").

    Named -INTERNAL so it cannot be mistaken for the client's copy if it is
    ever forwarded. The client copy is still one click away.

    Any version, not just the current one, and any status: this is the
    staff record of what a document says, and a draft is exactly the thing
    somebody needs to read before sending it.
    """
    _get_booking_or_404(db, booking_id)
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    html = templates.get_template("document.html").render(
        document=document, booking=document.booking, is_pdf=True, is_staff_preview=True
    )
    doc_label = "Agreement" if document.type.value == "agreement" else "BEO"
    filename = f"{document.booking.reference_code}-{doc_label}-v{document.version}-INTERNAL.pdf"
    return Response(
        content=render_html_to_pdf(html),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


def _get_draft_document_or_404(db: Session, booking_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    if document.status != DocumentStatus.draft:
        raise HTTPException(
            status_code=409, detail=f"cannot edit a document that is already {document.status.value}"
        )
    return document


def _edit_form_response(
    request, staff, db, booking_id, document, *, form_content, conflicts=(), conflict=False, status_code=200
):
    """The edit screen. Shared by the GET and by the conflict response, so a
    refused save comes back as the same form carrying the staff member's own
    words -- not a JSON error that throws their typing away."""
    template = (
        "admin/document_edit_agreement.html"
        if document.type == DocumentType.agreement
        else "admin/document_edit_beo.html"
    )
    # Only on the document approval would actually write to. A superseded
    # (but still draft) version renders no panel, so the page cannot show
    # one document's values above a form that edits another.
    current_draft = (
        beo_proposals_service.current_draft_beo(db, booking_id) if document.type == DocumentType.beo else None
    )
    on_current = current_draft is not None and current_draft.id == document.id
    proposal = beo_proposals_service.pending_proposal(db, booking_id) if on_current else None
    return templates.TemplateResponse(
        request,
        template,
        admin_ctx(
            request,
            staff,
            document=document,
            booking=document.booking,
            # What the form renders from: the stored content on a GET, and on
            # a refused save the staff member's own submission.
            form_content=form_content,
            conflicts=list(conflicts),
            conflict=conflict,
            vendor_types=[vt.value for vt in VendorType],
            # Carried back on save and compared: the form is rendered from
            # values that may be minutes old, and without this a change
            # somebody else committed in between is silently reverted -- and
            # now also recorded as the reverting staff member's own words.
            content_expect=documents_service.content_fingerprint(
                document.content, document_regeneration.PROTECTED_FIELD_NAMES
            ),
            # What the AI has proposed for this Event Order and has not yet
            # had approved -- shown against the value each would replace.
            beo_proposal=proposal if proposal is not None and proposal.is_reviewable else None,
            beo_review_rows=beo_proposals_service.review_rows(db, booking_id, document=current_draft)
            if on_current
            else [],
            beo_proposal_stale=(
                proposal is not None
                and proposal.document_id is not None
                and current_draft is not None
                and proposal.document_id != current_draft.id
            ),
        ),
        status_code=status_code,
    )


@router.get("/{booking_id}/documents/{document_id}/edit", response_class=HTMLResponse)
def edit_document_form(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    document = _get_draft_document_or_404(db, booking_id, document_id)
    return _edit_form_response(request, staff, db, booking_id, document, form_content=document.content)


def _submitted_food_order(descriptions, quantities, unit_prices, categories) -> dict:
    """The food order THIS form just submitted, in the shape it was typed.

    NOT the stored shape, which an earlier version of this line claimed: a
    quantity stays the string the browser sent rather than becoming an int,
    because the refused form is re-rendered from this and a half-typed "4x"
    has to come back as "4x". The comparison is what adapts --
    document_regeneration.differing_protected_fields asks the food order's
    own renderer, so the two shapes are one value.

    Only for the conflict screen: it is what that comparison reads and
    what the refused form is re-rendered from. Without it, a refusal caused
    by the food order names nothing in its table and hands the staff member
    back the STORED lines instead of the ones they just typed -- proved live
    before this existed.

    Lenient on purpose, and deliberately NOT the strict parse below that
    writes the document. That one raises 422 on a half-typed price, and a
    409 page whose entire job is handing somebody's typing back must not be
    the thing that throws it away.
    """
    categories = list(categories) + [""] * (len(descriptions) - len(categories))
    line_items = []
    for description, quantity, unit_price, category in zip(descriptions, quantities, unit_prices, categories):
        if not description.strip():
            continue
        entry = {"description": description.strip(), "quantity": quantity, "unit_price": unit_price}
        if category in ("platter", "pizza", "side", "dessert"):
            entry["category"] = category
        line_items.append(entry)
    return {"line_items": line_items, "note": None if line_items else f"{REVIEW} no food order captured yet"}


@router.post("/{booking_id}/documents/{document_id}/edit", dependencies=[Depends(require_csrf)])
def save_document_edit(
    booking_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    headings: list[str] = Form(default=[]),
    bodies: list[str] = Form(default=[]),
    catering_order_and_service_style: str = Form(default=""),
    bar_structure: str = Form(default=""),
    room_layout_notes: str = Form(default=""),
    music: str = Form(default=""),
    entertainment: str = Form(default=""),
    music_entertainment: str = Form(default=""),
    special_notes: str = Form(default=""),
    dietaries: str = Form(default=""),
    accessibility: str = Form(default=""),
    decorations: str = Form(default=""),
    status_text: str = Form(default=""),
    onsite_contact: str = Form(default=""),
    internal_notes: str = Form(default=""),
    av_video_slideshow: str | None = Form(default=None),
    av_microphones: str | None = Form(default=None),
    av_notes: str = Form(default=""),
    guest_arrival_time: str = Form(default=""),
    pack_down_notes: str = Form(default=""),
    moment_times: list[str] = Form(default=[]),
    moment_labels: list[str] = Form(default=[]),
    vendor_types: list[str] = Form(default=[]),
    vendor_names: list[str] = Form(default=[]),
    vendor_contacts: list[str] = Form(default=[]),
    vendor_bump_ins: list[str] = Form(default=[]),
    item_descriptions: list[str] = Form(default=[]),
    item_quantities: list[str] = Form(default=[]),
    item_unit_prices: list[str] = Form(default=[]),
    item_categories: list[str] = Form(default=[]),
    content_expect: str = Form(default=""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Only the negotiable wording is editable. Everything derived from the
    booking itself (dates, times, guest counts, reference) is deliberately
    left out of the form and carried through untouched -- a contract that
    could silently drift from the live booking record would be worse than
    one that can't be hand-tweaked at all."""
    _get_booking_or_404(db, booking_id)
    document = _get_draft_document_or_404(db, booking_id, document_id)
    # Locked BEFORE the content is read, so the values this form's save is
    # compared against are the ones it is actually replacing. Without it a
    # change committed between the page load and the save is reverted AND
    # recorded as this staff member's own words (see lock_draft_for_update).
    try:
        documents_service.lock_draft_for_update(db, document)
    except ValueError as exc:
        # Sent or signed between the check above and the locked re-read.
        # The same condition a moment earlier is a 409 from
        # _get_draft_document_or_404, so it is a 409 here too rather than
        # an unhandled ValueError and a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if content_expect != documents_service.content_fingerprint(
        document.content, document_regeneration.PROTECTED_FIELD_NAMES
    ):
        # Refuse, but hand their own words back. Throwing a JSON error at a
        # staff member who has just typed a long note would protect one
        # person's writing by destroying another's, in a feature whose whole
        # purpose is that neither happens. The form comes back with what
        # they wrote, a fresh fingerprint, and a table naming exactly what
        # saving again would replace -- tell me before, not after. An empty
        # value (a tab opened before this shipped) lands here too.
        submitted = {
            "catering_order_and_service_style": catering_order_and_service_style.strip(),
            "bar_structure": bar_structure.strip(),
            "room_layout_notes": room_layout_notes.strip(),
            "music": music.strip(),
            "entertainment": entertainment.strip(),
            "music_entertainment": music_entertainment.strip(),
            "special_notes": special_notes.strip(),
            "dietaries": dietaries.strip(),
            "accessibility": accessibility.strip(),
            "decorations": decorations.strip(),
            "status_text": status_text.strip(),
            "onsite_contact": onsite_contact.strip(),
            "internal_notes": internal_notes.strip(),
            # The food order too, now that the fingerprint covers it. Without
            # this the refusal names nothing when the food order is what
            # moved, and hands back the stored lines over the ones this staff
            # member just typed.
            "food_order": _submitted_food_order(
                item_descriptions, item_quantities, item_unit_prices, item_categories
            ),
        }
        if document.type == DocumentType.agreement:
            submitted = {
                "terms_sections": [
                    {"heading": heading.strip(), "body": body.strip()}
                    for heading, body in zip(headings, bodies)
                    if heading.strip() or body.strip()
                ]
            }
        stored = document.content if isinstance(document.content, dict) else {}
        # The labels the regenerate screen already shows for these fields,
        # so one document's field is called the same thing on both screens.
        labels = {spec.name: spec.label for spec in document_regeneration.PROTECTED_FIELDS}
        # What this save would REPLACE -- not what it would record as
        # authored. Asking changed_fields, which is the authorship question,
        # meant a submission that writes the generator's placeholder over
        # somebody's real text counted as nothing, so the table came back
        # empty and the page said nothing at all (review of 84916a7).
        # differing_fields is the same normalisation without that skip.
        #
        # Through each field's own renderer, which is what
        # differing_protected_fields adds: this form posts a quantity as the
        # string "4" where the document stores the int 4, so a raw compare
        # named the food order on EVERY refusal with identical text in both
        # columns.
        moved = document_regeneration.differing_protected_fields(stored, submitted)
        # Rendered through each field's own renderer, so an agreement's
        # terms_sections reads as its clauses rather than as a repr of a
        # list of dicts -- this is the screen somebody decides the fate of
        # a hand-negotiated contract on.
        specs = {spec.name: spec for spec in document_regeneration.PROTECTED_FIELDS}
        conflicts = []
        for name in sorted(moved):
            spec = specs.get(name)
            render = spec.render if spec is not None else str
            conflicts.append(
                {
                    "label": labels.get(name, name.replace("_", " ").capitalize()),
                    "stored": render(stored.get(name)),
                    "yours": render(submitted[name]),
                }
            )
        return _edit_form_response(
            request,
            staff,
            db,
            booking_id,
            document,
            # Every submitted value, including the empty ones. Overlaying only
            # the truthy ones put back the text of a field the staff member had
            # just cleared, so the form came back holding words they had
            # deleted and saving again restored them -- the exact silent
            # reversion this whole change exists to stop.
            form_content={**stored, **submitted},
            conflicts=conflicts,
            # Separate from the row list: a refusal must never be silent, and
            # the rows can legitimately come back empty (a formatting-only
            # change moves the fingerprint without changing any value this
            # comparison considers different).
            conflict=True,
            status_code=409,
        )

    content = dict(document.content)

    if document.type == DocumentType.agreement:
        sections = [
            {"heading": heading.strip(), "body": body.strip()}
            for heading, body in zip(headings, bodies)
            # A clause blanked out entirely is how the form deletes one.
            if heading.strip() or body.strip()
        ]
        if not sections:
            raise HTTPException(status_code=422, detail="An agreement needs at least one terms section")
        content["terms_sections"] = sections
        content["terms_text"] = rebuild_terms_text(sections)
    else:
        booking = document.booking
        line_items = []
        categories = list(item_categories) + [""] * (len(item_descriptions) - len(item_categories))
        for description, quantity, unit_price, category in zip(
            item_descriptions, item_quantities, item_unit_prices, categories
        ):
            if not description.strip():
                continue  # a blanked-out row is how the form deletes a line item
            try:
                entry = {
                    # "description", matching what the wizard and invoicing
                    # both write -- see the note in document.html.
                    "description": description.strip(),
                    "quantity": int(quantity),
                    "unit_price": str(Decimal(unit_price)),
                }
            except (ValueError, InvalidOperation) as exc:
                raise HTTPException(
                    status_code=422, detail=f"'{description.strip()}' needs a whole-number quantity and a valid price"
                ) from exc
            if category in ("platter", "pizza", "side", "dessert"):
                entry["category"] = category
            line_items.append(entry)

        # Timeline facts write through to the Booking itself (per-field
        # audit events), then the document's timeline is rebuilt from the
        # booking via the same shared builder generation uses -- the edit
        # screen and generation can't drift apart.
        try:
            parsed_arrival = dt.time.fromisoformat(guest_arrival_time) if guest_arrival_time.strip() else None
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid guest arrival time")
        moments = []
        for m_time, m_label in zip(moment_times, moment_labels):
            if not m_label.strip():
                continue  # a blanked-out label is how the form deletes a moment
            moments.append({"time": m_time.strip() or None, "label": m_label.strip()[:120]})
        timeline_changes = {
            "guest_arrival_time": parsed_arrival,
            "key_moments": moments or None,
            "pack_down_notes": pack_down_notes.strip() or None,
        }
        for field, new_value in timeline_changes.items():
            old_value = getattr(booking, field)
            if old_value != new_value:
                db.add(
                    BookingEvent(
                        booking_id=booking.id,
                        event_type="field_changed",
                        field_name=field,
                        old_value=str(old_value) if old_value is not None else None,
                        new_value=str(new_value) if new_value is not None else None,
                        actor=_actor(staff),
                    )
                )
                setattr(booking, field, new_value)

        # Vendors write through to booking_vendors. Staff-entered rows are
        # tagged source='staff' so a client re-saving their wizard step
        # never touches them; edits to an existing row keep its source and
        # its confirmation unless the bump-in time itself changed.
        existing_vendors = {(v.vendor_type, v.name): v for v in booking.vendors}
        seen_vendor_keys = set()
        valid_vendor_types = {vt.value for vt in VendorType}
        for v_type, v_name, v_contact, v_bump in zip(vendor_types, vendor_names, vendor_contacts, vendor_bump_ins):
            if not v_name.strip():
                continue  # a blanked-out name is how the form deletes a vendor
            if v_type not in valid_vendor_types:
                raise HTTPException(status_code=422, detail=f"Unknown vendor type '{v_type}'")
            try:
                parsed_bump = dt.time.fromisoformat(v_bump) if v_bump.strip() else None
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid bump-in time for '{v_name.strip()}'")
            key = (v_type, v_name.strip())
            seen_vendor_keys.add(key)
            row = existing_vendors.get(key)
            if row is None:
                db.add(
                    BookingVendor(
                        booking_id=booking.id,
                        vendor_type=v_type,
                        name=v_name.strip(),
                        contact_number=v_contact.strip() or None,
                        bump_in_time=parsed_bump,
                        bump_in_confirmed=False if parsed_bump is not None else None,
                        source="staff",
                    )
                )
            else:
                row.contact_number = v_contact.strip() or None
                if row.bump_in_time != parsed_bump:
                    row.bump_in_time = parsed_bump
                    row.bump_in_confirmed = False if parsed_bump is not None else None
        for key, row in existing_vendors.items():
            if key not in seen_vendor_keys:
                db.delete(row)
        db.flush()
        db.expire(booking, ["vendors"])

        vendors_snapshot = build_vendor_snapshot(booking.vendors)
        content["vendors"] = vendors_snapshot
        content["event_timeline"] = build_event_timeline(booking, vendors_snapshot)

        content["catering_order_and_service_style"] = catering_order_and_service_style.strip()
        content["bar_structure"] = bar_structure.strip()
        content["room_layout_notes"] = room_layout_notes.strip()
        # Split fields; the legacy merged field is cleared once staff save
        # through the new form (its value was offered as the music
        # field's prefill).
        content["music"] = music.strip() or None
        content["entertainment"] = entertainment.strip() or None
        content["music_entertainment"] = music_entertainment.strip() or None
        content["special_notes"] = special_notes.strip()
        content["dietaries"] = dietaries.strip() or NO_DIETARIES
        content["accessibility"] = accessibility.strip() or None
        content["decorations"] = decorations.strip() or None
        content["status_text"] = status_text.strip() or None
        content["onsite_contact"] = onsite_contact.strip() or None
        content["internal_notes"] = internal_notes.strip() or None
        if content.get("av"):
            av = dict(content["av"])
            av["video_slideshow"] = av_video_slideshow is not None
            av["microphones_for_speeches"] = av_microphones is not None
            av["notes"] = av_notes.strip() or None
            content["av"] = av
        content["food_order"] = {
            "line_items": line_items,
            "note": None if line_items else f"{REVIEW} no food order captured yet",
        }

        # Recomputed from the edited lines, never carried over stale. The
        # deposit already recorded stays as-is (an edit to the food order
        # says nothing about what's been paid).
        existing_deposit = (content.get("total_food_spend") or {}).get("deposit_paid")
        content["total_food_spend"] = build_total_food_spend(
            compute_food_order_total(line_items),
            Decimal(existing_deposit) if existing_deposit is not None else None,
        )

    # The staff edit form re-posts every field it renders, prefilled, so the
    # fields that are a person's words are named here and the writer records
    # only the ones this save actually changes. PROTECTED_FIELD_NAMES is the
    # same set the regenerate guard asks about, which is the point: the guard
    # stops guessing which of them a human wrote.
    documents_service.update_content(
        db,
        document,
        content,
        actor=_actor(staff),
        authored_fields=document_regeneration.PROTECTED_FIELD_NAMES,
        placeholders=document_regeneration.GENERATED_PLACEHOLDERS,
    )
    return _redirect_to_detail(booking_id)


# --- Event Order proposals: propose-and-approve --------------------------------
#
# The AI proposes values (app.api.ai_write); nothing reaches the document
# until a staff member approves it here, field by field or all at once. The
# textarea carries whatever Aaron actually wants written, so approving an
# edited value is one action rather than approve-then-fix -- and the edit
# is recorded, because it is the measure of whether the proposals are any
# good.


@router.post("/{booking_id}/beo-proposals/{proposal_id}/review", dependencies=[Depends(require_csrf)])
def review_beo_proposal(
    booking_id: uuid.UUID,
    proposal_id: uuid.UUID,
    request: Request,
    action: str = Form(...),
    # None, not "": a field whose box was not submitted keeps what was
    # proposed, while a box someone actually emptied is a real change the
    # house rules then refuse (2026-09-06 review -- defaulting these to ""
    # let a request with no textarea blank a declared allergy).
    value_catering_order_and_service_style: str | None = Form(default=None),
    value_bar_structure: str | None = Form(default=None),
    value_room_layout_notes: str | None = Form(default=None),
    value_music: str | None = Form(default=None),
    value_entertainment: str | None = Form(default=None),
    value_dietaries: str | None = Form(default=None),
    value_accessibility: str | None = Form(default=None),
    value_decorations: str | None = Form(default=None),
    value_special_notes: str | None = Form(default=None),
    value_onsite_contact: str | None = Form(default=None),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    proposal = db.get(BeoProposal, proposal_id)
    if proposal is None or proposal.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Proposal not found on this booking")

    submitted = {
        k: v for k, v in {
        "catering_order_and_service_style": value_catering_order_and_service_style,
        "bar_structure": value_bar_structure,
        "room_layout_notes": value_room_layout_notes,
        "music": value_music,
        "entertainment": value_entertainment,
        "dietaries": value_dietaries,
        "accessibility": value_accessibility,
        "decorations": value_decorations,
        "special_notes": value_special_notes,
        "onsite_contact": value_onsite_contact,
        }.items() if v is not None
    }

    verb, _, target = action.partition(":")
    try:
        if verb == "approve_all":
            beo_proposals_service.approve_all(db, proposal, actor=_actor(staff), values=submitted)
        elif verb in ("approve", "reject"):
            try:
                field_id = uuid.UUID(target)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Unknown proposal field") from exc
            field_row = db.get(BeoProposalField, field_id)
            if field_row is None or field_row.proposal_id != proposal.id:
                raise HTTPException(status_code=404, detail="Proposal field not found on this proposal")
            if verb == "approve":
                beo_proposals_service.approve_field(
                    db, field_row, actor=_actor(staff), value=submitted.get(field_row.field)
                )
            else:
                beo_proposals_service.reject_field(db, field_row, actor=_actor(staff))
        else:
            raise HTTPException(status_code=422, detail="Unknown action")
    except beo_proposals_service.ProposalError as exc:
        # Nothing is half-applied: the field rows and audit rows this
        # attempt added are discarded before the response is built.
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # documents.update_content_fields refuses anything not a draft.
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    document = beo_proposals_service.current_draft_beo(db, booking_id)
    if document is not None:
        return RedirectResponse(
            url=f"/admin/bookings/{booking_id}/documents/{document.id}/edit", status_code=303
        )
    return _redirect_to_detail(booking_id)


# Sentinel for _refresh_draft_beo_timeline: rebuild the vendor snapshot from
# the booking rather than keeping the one already stored.
_REBUILD_VENDORS = object()


def _refresh_draft_beo_timeline(db: Session, booking, *, actor: str, vendors=None) -> None:
    """Re-say the run sheet after confirming something it reports.

    Setup access and a vendor's bump-in both print as "requested, pending
    confirmation" until staff confirm them, and both are composed into the
    stored Event Order at generation. Confirming the fact without
    re-composing the document leaves the document saying pending forever --
    which is why HAM-20260911-AKPSO and HAM-20260912-2R11Q still read
    "requested, pending confirmation" after Aaron had confirmed the times to
    both clients in writing. The bump-in handler always did this refresh;
    the setup-access handler never did, and nothing made that asymmetry
    visible. One function now, so the next confirm-style action cannot
    quietly skip it.

    A sent or signed document is NEVER mutated -- staff regenerate, per the
    existing document rules -- so this is a no-op on anything but a draft.
    An already-sent Event Order goes on saying "requested" until it is
    regenerated. That is the correct answer for a document a client already
    holds, not a gap in this function.

    `vendors` defaults to keeping whatever snapshot is stored: confirming
    setup access should change the setup access line and nothing else. Pass
    _REBUILD_VENDORS to rebuild it from the booking, which is what
    confirming a bump-in needs.

    No authored_fields on the write: both keys are machine-derived, and
    recording them as a person's words would stop the next regenerate
    rebuilding the very values this refresh exists to keep current.
    """
    if booking is None:
        return
    current_beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    if current_beo is None or current_beo.status != DocumentStatus.draft:
        return
    content = dict(current_beo.content)
    if vendors is _REBUILD_VENDORS:
        content["vendors"] = build_vendor_snapshot(booking.vendors)
    content["event_timeline"] = build_event_timeline(booking, content.get("vendors"))
    documents_service.update_content(db, current_beo, content, actor=actor)


@router.post("/{booking_id}/vendors/{vendor_id}/confirm-bump-in", dependencies=[Depends(require_csrf)])
def confirm_vendor_bump_in(
    booking_id: uuid.UUID,
    vendor_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The staff half of the request/confirm handshake: a client's
    nominated bump-in time renders as "requested" everywhere until this
    is clicked -- same semantics as confirming early setup access."""
    _get_booking_or_404(db, booking_id)
    vendor = db.get(BookingVendor, vendor_id)
    if vendor is None or vendor.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Vendor not found on this booking")
    if vendor.bump_in_time is None:
        raise HTTPException(status_code=422, detail="This vendor has no bump-in time to confirm")
    vendor.bump_in_confirmed = True
    db.add(
        BookingEvent(
            booking_id=booking_id,
            event_type="vendor_bump_in_confirmed",
            field_name=vendor.vendor_type,
            new_value=f"{vendor.name} bump-in {vendor.bump_in_time.strftime('%H:%M')} confirmed",
            actor=_actor(staff),
        )
    )
    db.commit()

    _refresh_draft_beo_timeline(
        db, db.get(Booking, booking_id), actor=_actor(staff), vendors=_REBUILD_VENDORS
    )
    return _redirect_to_detail(booking_id)


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

    line_items = _parse_invoice_line_items(description, quantity, unit_price)

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


def _parse_invoice_line_items(
    description: list[str], quantity: list[str], unit_price: list[str]
) -> list[dict]:
    """Shared by the create and edit invoice forms: turns the parallel
    description/quantity/unit_price arrays into line-item dicts, skipping
    blank rows and rejecting unparseable numbers. A negative unit_price is
    allowed on purpose -- that's how a discount line is entered."""
    line_items = []
    for desc, qty, price in zip(description, quantity, unit_price):
        desc = desc.strip()
        if not desc:
            continue
        try:
            parsed_qty = Decimal(qty)
            parsed_price = Decimal(price)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail=f"Invalid quantity or unit price for line item '{desc}'")
        line_items.append({"description": desc, "quantity": str(parsed_qty), "unit_price": str(parsed_price)})
    if not line_items:
        raise HTTPException(status_code=422, detail="At least one line item is required")
    return line_items


@router.get("/{booking_id}/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def edit_invoice_form(
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
    if invoice.status != InvoiceStatus.draft:
        # Only drafts are editable; a sent invoice is revised (cancel +
        # reissue), so bounce back rather than show an editor that would
        # refuse the save anyway.
        return _redirect_to_detail(booking_id)
    # Show the staff-editable charge lines only -- the auto deposit credit
    # is re-derived on save, never hand-edited.
    editable_lines = invoicing._charge_lines(invoice.line_items)
    deposit_credit = next(
        (li for li in invoice.line_items if li.get("description") == invoicing.DEPOSIT_CREDIT_DESCRIPTION), None
    )
    return templates.TemplateResponse(
        request,
        "admin/invoice_edit.html",
        admin_ctx(
            request,
            staff,
            booking=invoice.booking,
            invoice=invoice,
            editable_lines=editable_lines,
            deposit_credit=deposit_credit,
        ),
    )


@router.post("/{booking_id}/invoices/{invoice_id}/edit", dependencies=[Depends(require_csrf)])
def edit_invoice(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    due_date: dt.date = Form(...),
    description: list[str] = Form(...),
    quantity: list[str] = Form(...),
    unit_price: list[str] = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    _get_booking_or_404(db, booking_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Invoice not found on this booking")
    line_items = _parse_invoice_line_items(description, quantity, unit_price)
    try:
        invoicing.update_invoice(db, invoice, line_items=line_items, due_date=due_date, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/invoices/{invoice_id}/revise", dependencies=[Depends(require_csrf)])
def revise_invoice(
    booking_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Cancel a sent invoice and reopen it as a fresh draft, landing the
    staff straight on that draft's editor to make the change (e.g. a
    discount) and re-send."""
    _get_booking_or_404(db, booking_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Invoice not found on this booking")
    try:
        new_draft = invoicing.revise_sent_invoice(db, invoice, actor=_actor(staff))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/admin/bookings/{booking_id}/invoices/{new_draft.id}/edit", status_code=303
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
    received_date: str | None = Form(None),
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

    # Optional back-dating: staff record a payment that actually arrived
    # earlier (a bank transfer that landed last week). Blank means "today",
    # keeping the common case a single click. Stored at midday UTC so the
    # calendar date can't slip either side of the venue's local day.
    received_at = None
    if received_date and received_date.strip():
        try:
            received_at = dt.datetime.combine(
                dt.date.fromisoformat(received_date.strip()), dt.time(12, 0), tzinfo=dt.timezone.utc
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid payment date")

    try:
        invoicing.record_payment(
            db,
            invoice,
            amount=parsed_amount,
            method=method,
            reference=reference or None,
            payer_name=payer_name or None,
            received_at=received_at,
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
    # Confirming the booking fact is only half the job: the Event Order
    # composed "requested, pending confirmation" into its run sheet at
    # generation, and without this it goes on saying so.
    _refresh_draft_beo_timeline(db, booking, actor=_actor(staff))
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


@router.post("/{booking_id}/policy/agreed-food-minimum", dependencies=[Depends(require_csrf)])
def set_agreed_food_minimum(
    booking_id: uuid.UUID,
    request: Request,
    agreed_min_food_spend: Decimal = Form(...),
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
        booking_service.set_agreed_food_minimum(
            db, booking, agreed_min_food_spend=agreed_min_food_spend, reason=parsed_reason, actor=_actor(staff)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _redirect_to_detail(booking_id)


@router.post("/{booking_id}/policy/bar-credit", dependencies=[Depends(require_csrf)])
def set_bar_credit(
    booking_id: uuid.UUID,
    request: Request,
    bar_credit: Decimal = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        booking_service.set_bar_credit(db, booking, bar_credit=bar_credit, actor=_actor(staff))
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


@router.get("/{booking_id}/enquiry-notification/preview", response_class=HTMLResponse)
def preview_enquiry_notification(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    """Shows exactly what the venue's new-enquiry notification for this
    booking says. Deliberately works whether or not Gmail is configured
    and whether or not this booking ever had one sent -- the question
    "what would this email say" is worth answering on its own."""
    booking = _get_booking_or_404(db, booking_id)
    recipient, subject, body, booking_url = enquiry_classification.preview_enquiry_notification(booking)
    contact = booking.contact
    return templates.TemplateResponse(
        request,
        "admin/enquiry_notification_preview.html",
        admin_ctx(
            request,
            staff,
            booking=booking,
            recipient=recipient,
            subject=subject,
            body=body,
            booking_url=booking_url,
            reply_to=contact.email if contact and is_valid_email(contact.email) else None,
        ),
    )


@router.post("/{booking_id}/enquiry-notification/resend", dependencies=[Depends(require_csrf)])
def resend_enquiry_notification(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    booking = _get_booking_or_404(db, booking_id)
    try:
        enquiry_classification.resend_enquiry_notification(db, booking, actor=_actor(staff))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Resend failed: {exc}") from exc
    return _redirect_to_detail(booking_id)
