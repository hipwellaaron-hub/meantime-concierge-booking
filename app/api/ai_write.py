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

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.ai_auth import AiContext, require_ai_write
from app.models import AiRequestKind, Booking, Space
from app.services import ai_access, beo_proposals, beo_rules

router = APIRouter(prefix="/api/ai", tags=["ai-write"])


class EventOrderProposalIn(BaseModel):
    # `model` is the name the drafting layer already uses for "which model
    # produced this"; pydantic's protected namespace is turned off here
    # rather than renaming the field in the AI's contract.
    model_config = ConfigDict(protected_namespaces=())

    source: str = Field(min_length=3, max_length=500)
    fields: dict[str, str] = Field(min_length=1)
    trigger: str | None = Field(default=None, max_length=30)
    model: str | None = Field(default=None, max_length=80)


def _booking_by_reference(ctx: AiContext, reference: str) -> Booking:
    booking = ctx.db.scalars(
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == ctx.venue.id, Booking.reference_code == reference.strip())
    ).first()
    if booking is None:
        raise HTTPException(status_code=404, detail=f"No booking {reference.strip()!r} at this venue")
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
    unknown = sorted(set(payload.fields) - set(beo_rules.PROPOSABLE_FIELDS))
    if unknown:
        # Answered before anything is stored: a caller aiming at the food
        # order or a total has misread the contract, and the useful reply
        # is the list of fields that do exist.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "not a proposable field",
                "unknown_fields": unknown,
                "proposable_fields": list(beo_rules.PROPOSABLE_FIELDS),
                "note": "Status, the food order and every total are computed from the catalogue, the wizard "
                        "and the booking. They are not proposable.",
            },
        )

    proposal, result = beo_proposals.propose(
        ctx.db,
        booking,
        fields=payload.fields,
        source=payload.source,
        actor=ctx.actor,
        trigger=payload.trigger,
        model=payload.model,
    )

    ai_access.log_request(
        ctx.db,
        kind=AiRequestKind.write,
        endpoint=f"/api/ai/bookings/{reference}/event-order-proposal",
        method="POST",
        params={"fields": sorted(payload.fields)},
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
                "note": "Stored for calibration, not shown to staff and not applied. Fix and re-propose.",
            },
        )

    return {
        "proposal_id": str(proposal.id),
        "reference": booking.reference_code,
        "status": proposal.status,
        "awaiting_approval": [f.field for f in proposal.pending_fields],
        "as_of": ctx.as_of_iso,
        "note": "Stored as a pending proposal. Nothing is applied until a staff member approves it on the "
                "Event Order form.",
    }


@router.get("/bookings/{reference}/event-order-proposal")
def read_event_order_proposal(reference: str, ctx: AiContext = Depends(require_ai_write)):
    """What is still awaiting approval, and what happened to the last ask.

    Behind the same write gate as proposing, deliberately: it exists so the
    proposer can see whether its own work landed, not as a general read.
    """
    booking = _booking_by_reference(ctx, reference)
    proposal = beo_proposals.pending_proposal(ctx.db, booking.id)
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
