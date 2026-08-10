import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document import DocumentStatus
from app.models.invoice import InvoiceStatus
from app.models.wizard_session import WizardSessionStatus, WizardStep
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.schemas.wizard import WizardBasicsStep, WizardBeverageStep, WizardExtrasStep, WizardFoodStep, WizardMusicStep
from app.services import wizard as wizard_service
from app.templating import templates
from app.utils import looks_like_a_token, truncate

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


@router.get("/w/{token}", response_class=HTMLResponse)
def view_wizard(token: str, request: Request, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    booking = session.booking

    if session.status == WizardSessionStatus.submitted:
        return templates.TemplateResponse(request, "wizard/submitted.html", {"session": session, "booking": booking})

    step_data = {
        WizardStep.basics: {
            "start_time": booking.start_time,
            "end_time": booking.end_time,
            "food_service_time": booking.food_service_time,
            "setup_access_time": booking.setup_access_time,
            "adult_count": booking.adult_count,
            "child_count": booking.child_count,
        },
        WizardStep.food: session.food_response,
        WizardStep.beverage: session.beverage_response,
        WizardStep.music: session.music_response,
        WizardStep.extras: session.extras_response,
        WizardStep.review: None,
    }[session.current_step]

    return templates.TemplateResponse(
        request,
        "wizard/step.html",
        {"session": session, "booking": booking, "current_step": session.current_step.value, "step_data": step_data},
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
            music_type=payload.music_type,
            notes=payload.notes,
            bump_in_notes=payload.bump_in_notes,
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


@router.post("/w/{token}/review", dependencies=[Depends(rate_limit_dependency(_wizard_step_rate_limiter))])
def submit_review_step(token: str, request: Request, db: Session = Depends(get_db)):
    session = _get_usable_session(db, token)
    try:
        session, generation = wizard_service.submit_review(db, session, actor=_actor(request))
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
