"""The one write the AI is allowed to make: proposing Event Order values.

It is a Tier 1 write in the brief's terms, and it is a write only in the
narrowest sense -- it writes a PROPOSAL. It cannot change an Event Order,
a booking, a status, a figure or a document. Approval is a staff action on
the Event Order form, and there is no endpoint here that performs one.

Everything else about the boundary is unchanged: the credential, the
kill switch, the write budget and the request log all come from
app.api.ai_auth.require_ai_write, and a proposal that fails the house
rules (app.services.beo_rules) is stored for calibration and refused with
the codes that failed, so the caller learns what to fix rather than
retrying blind.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.ai_auth import AiContext, require_ai, require_ai_write
from app.models import AiRequestKind, Booking, Space
from app.models.document import DocumentStatus, DocumentType
from app.services import ai_access, beo_proposals, beo_rules
from app.services import documents as documents_service

router = APIRouter(prefix="/api/ai", tags=["ai-write"])


class EventOrderProposalIn(BaseModel):
    # `model` is the name the drafting layer already uses for "which model
    # produced this"; pydantic's protected namespace is turned off here
    # rather than renaming the field in the AI's contract.
    model_config = ConfigDict(protected_namespaces=())

    source: str = Field(min_length=3, max_length=500)
    fields: dict[str, str] = Field(default_factory=dict)
    # Catalogue items and quantities -- never a price. Extra keys are let
    # through on purpose so a line that sneaks a price in is refused by
    # name (food_price_sent) rather than silently dropped by pydantic.
    food_order: list["FoodLineIn"] | None = Field(default=None, max_length=50)
    trigger: str | None = Field(default=None, max_length=30)
    model: str | None = Field(default=None, max_length=80)


class FoodLineIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    menu_item_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    quantity: int


def _booking_by_reference(ctx: AiContext, reference: str) -> Booking:
    """The booking this reference names, if this credential may touch it.

    Scoped to the PERMITTED SET, not to ctx.venue. ctx.venue is
    ai_venues(db)[0] -- the first venue by name -- which was the whole
    story while AI_VENUE_SLUG held one slug and became wrong the moment it
    held two: every proposal for a booking at the alphabetically-later
    venue answered "No booking ... at this venue", for a booking that
    exists and that the same credential can read.

    No venue ARGUMENT here, unlike the list reads. A reference names one
    booking, so the only question is authorisation; and reference_prefix is
    unique per venue, so the reference already says which building it is.
    Requiring an argument would be friction with no safety gain.
    """
    booking = ctx.db.scalars(
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id.in_([v.id for v in (ctx.venues or [ctx.venue])]),
            Booking.reference_code == reference.strip(),
        )
    ).first()
    if booking is None:
        raise HTTPException(
            status_code=404,
            detail=f"No booking {reference.strip()!r} at any venue this credential can reach",
        )
    return booking


@router.post("/bookings/{reference}/event-order-proposal", status_code=201)
def propose_event_order_values(
    reference: str, payload: EventOrderProposalIn, ctx: AiContext = Depends(require_ai_write)
):
    """Propose values for the ten free-text Event Order fields.

    Stores them as a pending draft against the booking. Applies nothing:
    each field waits for a staff approval on the Event Order form, where
    the proposed value is shown against the one it would replace.
    """
    booking = _booking_by_reference(ctx, reference)
    if not payload.fields and payload.food_order is None:
        raise HTTPException(status_code=422, detail="propose at least one text field, or a food_order, or both")
    try:
        proposal, result = beo_proposals.propose(
            ctx.db,
            booking,
            fields=payload.fields,
            source=payload.source,
            actor=ctx.actor,
            trigger=payload.trigger,
            model=payload.model,
            food_order=[line.model_dump() for line in payload.food_order] if payload.food_order is not None else None,
        )
    except beo_proposals.ProposalError as exc:
        # The booking cannot take a proposal at all (cancelled, or a linked
        # second room whose Event Order lives on the parent).
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    ai_access.log_request(
        ctx.db,
        kind=AiRequestKind.write,
        endpoint=f"/api/ai/bookings/{reference}/event-order-proposal",
        method="POST",
        params={"fields": sorted(payload.fields), "food_lines": len(payload.food_order or [])},
        status_code=422 if result.blocked else 201,
        booking_id=booking.id,
        trigger=payload.trigger,
        context=payload.source[:500],
    )

    if result.blocked:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "the proposal failed the Event Order house rules",
                "proposal_id": str(proposal.id),
                "rule_codes": result.codes,
                "violations": [
                    {"code": v.code, "field": v.field, "message": v.message, "excerpt": v.excerpt}
                    for v in result.violations
                ],
                "proposable_fields": list(beo_rules.PROPOSABLE_FIELDS),
                "food_order": "proposed separately as [{menu_item_id | name, quantity}] -- catalogue items only, never a price",
                "note": "Recorded against the booking for calibration and visible in its audit trail, but not "
                        "offered for approval and not applied. Fix and re-propose.",
            },
        )

    current = documents_service.get_current(ctx.db, booking.id, DocumentType.beo)
    created = bool(getattr(proposal, "draft_created", False))
    waiting_on = ""
    if current is None:
        waiting_on = (
            " The booking has no Event Order and is still an enquiry, so a staff member generates one "
            "before this can be reviewed."
        )
    elif current.status != DocumentStatus.draft:
        shown = "approved" if current.status == DocumentStatus.signed else current.status.value
        waiting_on = (
            f" The current Event Order (v{current.version}, {shown}) has already gone out, so a staff "
            "member must Revise it before this proposal can be reviewed."
        )
    elif created:
        waiting_on = " The booking had no Event Order, so this proposal created the first draft for review."
    return {
        "proposal_id": str(proposal.id),
        "reference": booking.reference_code,
        "status": proposal.status,
        "awaiting_approval": [f.field for f in proposal.pending_fields],
        "warnings": [{"code": w.code, "field": w.field, "message": w.message} for w in result.warnings],
        # What the proposal is waiting on. `created` is true when the
        # booking had no Event Order and this proposal made the first
        # draft (nothing is sent; a draft is invisible to the client).
        "event_order": (
            {"version": current.version, "status": current.status.value, "created": created}
            if current is not None
            else None
        ),
        "as_of": ctx.as_of_iso,
        "note": (
            "Stored as a pending proposal. Nothing is applied until a staff member approves it on the "
            "Event Order form." + waiting_on
        ),
    }


@router.get("/bookings/{reference}/event-order-proposal")
def read_event_order_proposal(reference: str, ctx: AiContext = Depends(require_ai)):
    """What is still awaiting approval, and what happened to the last ask.

    Both halves, which took reading the LATEST proposal rather than the
    latest PENDING one. applied_value and edited_before_approval -- the
    calibration signal this endpoint exists for, the difference between
    what was proposed and what a human actually approved -- are written as
    each field is decided, and deciding the last one resolves the
    proposal. Reading only pending rows therefore returned `proposal:
    null` at the exact moment the answer became available, and the tool
    description promised the opposite.

    `status` is the discriminator, not the presence of the object: pending
    means somebody still has to look, resolved/superseded/rules_blocked
    mean the ask is over. Per field, `state` says the same thing.

    A read, behind the read gate. It sat behind require_ai_write until the
    2026-09-06 review pointed out that enforce_write_budget MUTATES state:
    once the budget had tripped, this GET re-disabled writes the moment
    staff re-enabled them, and polling it after a 429 is the caller's most
    natural behaviour.
    """
    booking = _booking_by_reference(ctx, reference)
    proposal = beo_proposals.latest_proposal(ctx.db, booking.id)
    if proposal is None:
        return {"reference": booking.reference_code, "proposal": None, "as_of": ctx.as_of_iso}
    return {
        "reference": booking.reference_code,
        "as_of": ctx.as_of_iso,
        "proposal": {
            "proposal_id": str(proposal.id),
            "status": proposal.status,
            "source": proposal.source,
            "created_at": proposal.created_at.isoformat(),
            "fields": [
                {
                    "field": f.field,
                    "state": f.state,
                    "proposed_value": f.proposed_value,
                    "applied_value": f.applied_value,
                    "edited_before_approval": f.edited_before_approval,
                }
                for f in proposal.fields
            ],
        },
    }
