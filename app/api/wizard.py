import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document import DocumentStatus
from app.models.invoice import InvoiceStatus
from app.models.menu_item import MenuItemCategory
from app.models.wizard_session import WizardSessionStatus, WizardStep
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.schemas.wizard import (
    WizardAvStep,
    WizardBasicsStep,
    WizardBeverageStep,
    WizardExtrasStep,
    WizardFoodStep,
    WizardMusicStep,
    WizardReviewStep,
    WizardVendorsStep,
)
from app.services import catalogue
from app.services import wizard as wizard_service
from app.services.document_generation import format_day_date
from app.services.policy import AV_USB_DEADLINE_DAYS_BEFORE_EVENT, PLATTER_GUESTS_PER_PLATTER
from app.services.validation import MUSIC_OFF_TIME, SETUP_ACCESS_STANDARD_TIME
from app.templating import templates
from app.utils import format_date_dmy, looks_like_a_token, truncate

router = APIRouter(tags=["wizard"])

# One shared limiter across all six step-POST routes (not GET): a
# legitimate resumable session plausibly touches several steps in one
# sitting, so this is more generous than the 10/300s sign-document limiter.
_wizard_step_rate_limiter = InMemoryRateLimiter(max_requests=30, window_seconds=300)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _actor(request: Request) -> str:
    # No login exists on this public route -- the client is identified
    # only by IP for the audit trail, same spirit as documents.py's
    # signer_ip capture, truncated the same defensive way.
    return truncate(f"wizard_client:{client_ip(request)}", BOOKING_EVENT_ACTOR_MAX_LENGTH)


def _get_usable_session(db: Session, token: str):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Wizard link not found")
    session = wizard_service.get_by_token(db, token)
    if session is None or not wizard_service.is_usable(session):
        raise HTTPException(status_code=404, detail="Wizard link not found")
    return session


def _handle_value_error(exc: ValueError) -> HTTPException:
    if str(exc) == wizard_service.ALREADY_SUBMITTED_MESSAGE:
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _catalogue_payload(db: Session, booking, category: MenuItemCategory) -> list[dict]:
    # Items with no resolvable price for this booking (the undefined-
    # legacy-price edge case, e.g. Vegetarian Pizza on a pre-cutover
    # booking) are excluded from what a client can select at all --
    # better to not offer it than show a confusing blank/guessed price.
    payload = []
    for item in catalogue.get_active_items(db, category):
        price = catalogue.resolve_price(item, booking)
        if price is None:
            continue
        # Marker chips shown in the picker: the published dietary codes,
        # plus P only where an item actually contains peanuts (none on
        # the current function menu). NULL markers = not yet confirmed
        # against the website -- shown as nothing, never guessed.
        markers = list(item.dietary_markers or [])
        if item.contains_peanuts:
            markers.append("P")
        payload.append({"id": str(item.id), "name": item.name, "price": str(price), "markers": markers})
    return payload


@router.get("/w/{token}", response_class=HTMLResponse)
def view_wizard(token: str, request: Request, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    session = wizard_service.record_open(db, session)
    booking = session.booking

    if session.status == WizardSessionStatus.submitted:
        return templates.TemplateResponse(request, "wizard/submitted.html", {"session": session, "booking": booking})

    # The AV USB deadline is composed server-side as an absolute date
    # ("Thursday 27 August") -- never a relative phrase, which would go
    # stale the moment the page outlives the day it rendered.
    av_usb_deadline = None
    if booking.event_date is not None:
        import datetime as _dt

        av_usb_deadline = format_day_date(
            booking.event_date - _dt.timedelta(days=AV_USB_DEADLINE_DAYS_BEFORE_EVENT)
        )

    # Vendors are serialized from the authoritative BookingVendor rows,
    # not vendors_response -- so staff corrections show through when the
    # client reopens their wizard.
    wizard_vendor_rows = [v for v in booking.vendors if v.source == "wizard"]

    bootstrap = {
        "booking": {
            "event_name": booking.event_name,
            "reference_code": booking.reference_code,
            "event_date": booking.event_date.isoformat(),
            # Display-only sibling: the ISO value above stays for any code
            # that parses it; this one is what humans see.
            "event_date_display": format_date_dmy(booking.event_date),
            "space_name": booking.space.name,
            "start_time": str(booking.start_time) if booking.start_time else None,
            "end_time": str(booking.end_time) if booking.end_time else None,
            "food_service_time": str(booking.food_service_time) if booking.food_service_time else None,
            "setup_access_time": str(booking.setup_access_time) if booking.setup_access_time else None,
            "guest_arrival_time": str(booking.guest_arrival_time) if booking.guest_arrival_time else None,
            "key_moments": booking.key_moments or [],
            "adult_count": booking.adult_count,
            "child_count": booking.child_count,
            "outside_cake_permitted": booking.outside_cake_permitted,
        },
        # Server-driven step list: the AV step only exists for The Loft.
        "steps": [s.value for s in wizard_service.step_order_for(booking)],
        "current_step": session.current_step.value,
        "responses": {
            "food": session.food_response,
            "beverage": session.beverage_response,
            "music": session.music_response,
            "extras": session.extras_response,
            "vendors": (
                {
                    "vendors": [
                        {
                            "vendor_type": v.vendor_type,
                            "name": v.name,
                            "contact_number": v.contact_number,
                            "bump_in_time": str(v.bump_in_time)[:5] if v.bump_in_time else None,
                        }
                        for v in wizard_vendor_rows
                    ]
                }
                if session.vendors_response is not None
                else None
            ),
            "av": session.av_response,
        },
        "catalogue": {
            "platters": _catalogue_payload(db, booking, MenuItemCategory.platter),
            "pizzas": _catalogue_payload(db, booking, MenuItemCategory.pizza),
            "sides": _catalogue_payload(db, booking, MenuItemCategory.side),
            "desserts": _catalogue_payload(db, booking, MenuItemCategory.dessert),
            "cakes": _catalogue_payload(db, booking, MenuItemCategory.cake),
        },
        "policy": {
            "min_food_spend": str(booking.agreed_min_food_spend),
            "platter_guests_per_platter": PLATTER_GUESTS_PER_PLATTER,
            "setup_access_standard_time": str(SETUP_ACCESS_STANDARD_TIME),
            "music_off_time": str(MUSIC_OFF_TIME),
            "av_usb_deadline_display": av_usb_deadline,
        },
    }

    return templates.TemplateResponse(
        request,
        "wizard/wizard.html",
        # Passed as a dict and rendered with Jinja's |tojson, which escapes
        # <, >, & as \uXXXX -- so a client-supplied event name or vendor
        # name containing </script> can't break out of the JSON island and
        # inject markup. Never render this via json.dumps + |safe.
        {"booking": booking, "bootstrap": bootstrap},
    )


@router.post("/w/{token}/basics", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_basics_step(token: str, request: Request, payload: WizardBasicsStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        warnings = wizard_service.save_basics_step(
            db,
            session,
            start_time=payload.start_time,
            end_time=payload.end_time,
            food_service_time=payload.food_service_time,
            setup_access_time=payload.setup_access_time,
            adult_count=payload.adult_count,
            child_count=payload.child_count,
            guest_arrival_time=payload.guest_arrival_time,
            key_moments=[
                {"time": str(m.time)[:5] if m.time else None, "label": m.label} for m in payload.key_moments
            ],
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {
        "current_step": session.current_step.value,
        "warnings": [dataclasses.asdict(w) for w in warnings],
    }


@router.post("/w/{token}/food", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_food_step(token: str, request: Request, payload: WizardFoodStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        guidance = wizard_service.save_food_step(
            db,
            session,
            platters=[item.model_dump() for item in payload.platters],
            pizzas=[item.model_dump() for item in payload.pizzas],
            sides=[item.model_dump() for item in payload.sides],
            desserts=[item.model_dump() for item in payload.desserts],
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {
        "current_step": session.current_step.value,
        "guidance": {
            "message": guidance.message,
            "subtotal": str(guidance.subtotal),
            "min_food_spend": str(guidance.min_food_spend),
            "met_minimum_spend": guidance.met_minimum_spend,
            "shortfall": str(guidance.shortfall) if guidance.shortfall is not None else None,
            "expected_platter_range": guidance.expected_platter_range,
        },
    }


@router.post("/w/{token}/beverage", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_beverage_step(token: str, request: Request, payload: WizardBeverageStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        wizard_service.save_beverage_step(
            db,
            session,
            bar_structure=payload.bar_structure,
            bar_limit=payload.bar_limit,
            bar_inclusions=payload.bar_inclusions,
            bar_inclusions_note=payload.bar_inclusions_note,
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {"current_step": session.current_step.value}


@router.post("/w/{token}/music", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_music_step(token: str, request: Request, payload: WizardMusicStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        wizard_service.save_music_step(
            db,
            session,
            music_types=payload.music_types or None,
            music_type=payload.music_type,
            notes=payload.notes,
            bump_in_notes=payload.bump_in_notes,
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {"current_step": session.current_step.value}


@router.post("/w/{token}/vendors", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_vendors_step(token: str, request: Request, payload: WizardVendorsStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        wizard_service.save_vendors_step(
            db,
            session,
            vendors=[
                {
                    "vendor_type": v.vendor_type.value,
                    "name": v.name,
                    "contact_number": v.contact_number,
                    "bump_in_time": str(v.bump_in_time)[:5] if v.bump_in_time else None,
                }
                for v in payload.vendors
            ],
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {"current_step": session.current_step.value}


@router.post("/w/{token}/av", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_av_step(token: str, request: Request, payload: WizardAvStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        wizard_service.save_av_step(
            db,
            session,
            video_slideshow=payload.video_slideshow,
            microphones_for_speeches=payload.microphones_for_speeches,
            notes=payload.notes,
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {"current_step": session.current_step.value}


@router.post("/w/{token}/extras", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_extras_step(token: str, request: Request, payload: WizardExtrasStep, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        escalated = wizard_service.save_extras_step(
            db,
            session,
            cake_choice_type=payload.cake_choice_type,
            cake_menu_item_id=payload.cake_menu_item_id,
            cake_notes=payload.cake_notes,
            decorations_notes=payload.decorations_notes,
            layout_notes=payload.layout_notes,
            dietary_requirements=payload.dietary_requirements,
            accessibility_needs=payload.accessibility_needs,
            additional_notes=payload.additional_notes,
            actor=_actor(request),
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {"current_step": session.current_step.value, "accessibility_escalated": escalated}


@router.post("/w/{token}/save-for-later", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def save_for_later(token: str, request: Request, db: Session = Depends(get_db)):
    """The client is stepping away mid-wizard. Their answers are already
    saved per step; this records the intent, attempts the resume email,
    and returns everything the confirmation panel must show -- the due
    date as an ABSOLUTE date, the resume link, and the help address --
    because clients frequently don't check email straight away, and a
    panel that just says "saved" would strand them."""
    import datetime as _dt
    import logging

    from app.config import settings
    from app.models import BookingEvent
    from app.services import notifications
    from app.services.document_generation import format_day_date
    from app.services.policy import WIZARD_TRIGGER_DAYS_BEFORE_EVENT
    from app.utils import truncate as _truncate

    logger = logging.getLogger(__name__)
    session = _get_usable_session(db, token)
    booking = session.booking

    due_date_display = None
    if booking.event_date is not None:
        due_date_display = format_day_date(
            booking.event_date - _dt.timedelta(days=WIZARD_TRIGGER_DAYS_BEFORE_EVENT)
        )
    resume_url = f"{settings.dashboard_base_url}/w/{session.access_token}"

    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_saved_for_later", actor=_actor(request)))

    email_sent = False
    try:
        notifications.send_wizard_resume_email(booking, resume_url=resume_url, due_date_display=due_date_display)
        email_sent = True
        db.add(BookingEvent(booking_id=booking.id, event_type="wizard_resume_email_sent", actor=_actor(request)))
    except Exception as exc:  # noqa: BLE001 -- a mail problem must never lose the client's progress
        logger.warning("Wizard resume email failed for %s: %s", booking.reference_code, exc)
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_resume_email_failed",
                new_value=_truncate(str(exc), 500),
                actor=_actor(request),
            )
        )
    db.commit()

    return {
        "email_sent": email_sent,
        "resume_url": resume_url,
        "due_date_display": due_date_display,
        "help_email": notifications.ENQUIRY_NOTIFICATION_RECIPIENT,
        "contact_email": booking.contact.email if booking.contact else None,
    }


@router.post("/w/{token}/review", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_review_step(
    token: str, request: Request, payload: WizardReviewStep | None = None, db: Session = Depends(get_db)
):
    session = _get_usable_session(db, token)
    try:
        session, generation = wizard_service.submit_review(
            db, session, actor=_actor(request), final_notes=payload.final_notes if payload else None
        )
    except ValueError as exc:
        raise _handle_value_error(exc) from exc

    return {
        "status": session.status.value,
        "is_clean": generation.is_clean,
        "outstanding_items": generation.outstanding_items,
        "beo_status": generation.document.status.value,
        "invoice_status": generation.invoice.status.value,
        # Only handed back once actually sent -- a draft document's token
        # isn't meant to be reachable yet, matching how its own /d and /i
        # routes already treat a draft as not-found.
        "invoice_url": f"/i/{generation.invoice.access_token}" if generation.invoice.status != InvoiceStatus.draft else None,
        "beo_url": f"/d/{generation.document.access_token}" if generation.document.status != DocumentStatus.draft else None,
    }
