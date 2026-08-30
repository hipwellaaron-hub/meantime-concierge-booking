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
from app.models import Booking, BookingEvent, BookingVendor, Document, Invoice, Space, Venue
from app.models.booking_vendor import VendorType
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentMethod
from app.models.staff_user import StaffUser
from app.models.wizard_session import WizardSessionStatus
from app.schemas.enquiry import EVENT_TYPES, EnquiryCreate
from app.services import booking as booking_service
from app.services.contact_matching import find_or_create_contact
from app.services import documents as documents_service
from app.services import enquiry_classification
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services import wizard_generation
from app.services.attribution import summarize_channel
from app.services.document_generation import (
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
            enquiry_notification_failed=any(
                e.event_type == "enquiry_notification_failed" for e in booking.events
            ) and booking.enquiry_notification_sent_at is None,
            first_touch_channel=summarize_channel(booking.first_touch_attribution),
            last_touch_channel=summarize_channel(booking.last_touch_attribution),
            touches_differ=booking.first_touch_attribution != booking.last_touch_attribution,
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
    """Attaches or replaces a booking's contact -- the recurring need
    app.services.ivvy_calendar_import creates by design: a booking
    imported with real space/time but no email at all only gets a real
    contact once staff track one down, often from a live email thread."""
    booking = _get_booking_or_404(db, booking_id)
    name, email = name.strip(), email.strip()
    if not name or not email:
        raise HTTPException(status_code=422, detail="Name and email are both required")
    contact, _duplicates = find_or_create_contact(db, name, email, (phone or "").strip() or None)
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


def _get_draft_document_or_404(db: Session, booking_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = db.get(Document, document_id)
    if document is None or document.booking_id != booking_id:
        raise HTTPException(status_code=404, detail="Document not found on this booking")
    if document.status != DocumentStatus.draft:
        raise HTTPException(
            status_code=409, detail=f"cannot edit a document that is already {document.status.value}"
        )
    return document


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
    template = (
        "admin/document_edit_agreement.html"
        if document.type == DocumentType.agreement
        else "admin/document_edit_beo.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        admin_ctx(
            request,
            staff,
            document=document,
            booking=document.booking,
            vendor_types=[vt.value for vt in VendorType],
        ),
    )


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
        content["dietaries"] = dietaries.strip() or "No dietary requirements declared"
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

    documents_service.update_content(db, document, content, actor=_actor(staff))
    return _redirect_to_detail(booking_id)


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

    # A current DRAFT BEO gets its stored vendor snapshot refreshed so the
    # document stops saying "requested". A sent/signed document is never
    # mutated -- staff regenerate, per the existing document rules.
    current_beo = documents_service.get_current(db, booking_id, DocumentType.beo)
    if current_beo is not None and current_beo.status == DocumentStatus.draft:
        booking = db.get(Booking, booking_id)
        content = dict(current_beo.content)
        snapshot = build_vendor_snapshot(booking.vendors)
        content["vendors"] = snapshot
        content["event_timeline"] = build_event_timeline(booking, snapshot)
        documents_service.update_content(db, current_beo, content, actor=_actor(staff))
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


@router.get("/{booking_id}/enquiry-notification/preview", response_class=HTMLResponse)
def preview_enquiry_notification(
    booking_id: uuid.UUID, request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    """Shows exactly what the venue's new-enquiry notification for this
    booking says. Deliberately works whether or not Gmail is configured
    and whether or not this booking ever had one sent -- the question
    "what would this email say" is worth answering on its own."""
    booking = _get_booking_or_404(db, booking_id)
    recipient, subject, body = enquiry_classification.preview_enquiry_notification(booking)
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
