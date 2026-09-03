"""Read endpoints for the AI integration (brief section 3).

Tier 0 only. There is no write route in this module and no disabled or
commented-out one either: the boundary in the brief is enforced by absence,
so a Tier 3 action returns 404 because nothing is listening, not 403
because something declined (brief sections 2, 6 and 12).

Every response carries `as_of`, and availability additionally carries
`day_of_week` and `open_enquiries`. Those three are not decoration -- they
are what makes the verification loop in section 10a possible: freshness,
the Saturday-that-is-a-Friday check, and "free but contested".
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.ai_auth import AiContext, require_ai
from app.database import get_db
from app.models import Booking, Contact, MenuItem, Space
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.services import ai_availability, ai_pipeline, policy
from app.services import catalogue as catalogue_service

router = APIRouter(prefix="/api/ai", tags=["ai-read"])

# Availability is cheap per day but not free; a year at a time is a
# runaway, not a question anyone asks.
MAX_AVAILABILITY_DAYS = 120


def _space_by_slug(db: Session, venue, slug: str | None) -> Space | None:
    """Accepts the friendly names the brief uses (loft / mezzanine /
    lounge) as well as the stored name."""
    if not slug:
        return None
    wanted = slug.strip().lower().replace("the ", "")
    spaces = ai_availability.spaces_for(db, venue)
    for space in spaces:
        if space.name.strip().lower().replace("the ", "") == wanted:
            return space
    raise HTTPException(status_code=400, detail=f"Unknown space '{slug}'")


# --- 3.0 pipeline -------------------------------------------------------


def _pipeline_payload(record: ai_pipeline.PipelineRecord) -> dict:
    b = record.booking
    return {
        "reference": b.reference_code,
        "id": str(b.id),
        "stage": record.stage,
        # The stored status is reported alongside the computed stage
        # because `archived` deliberately covers four distinct terminal
        # statuses plus "the event is in the past".
        "status": b.status.value,
        "awaiting": record.awaiting,
        "last_activity_at": record.last_activity_at.isoformat() if record.last_activity_at else None,
        "last_activity_by": record.last_activity_by,
        "days_at_stage": record.days_at_stage,
        "event_date": b.event_date.isoformat() if b.event_date else None,
        "day_of_week": ai_availability.day_of_week(b.event_date) if b.event_date else None,
        "space": b.space.name if b.space else None,
        "linked_spaces": [c.space.name for c in b.linked_bookings if c.space],
        "contact_name": b.contact.name if b.contact else None,
        "adults": b.adult_count,
        "contested": record.contested,
        "contested_with": record.contested_with,
    }


@router.get("/pipeline")
def pipeline(
    stage: str | None = Query(default=None),
    awaiting: str | None = Query(default=None),
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Every live booking and enquiry, grouped by computed stage.

    Archived records (terminal status, or an event date in the past) are
    excluded unless asked for by name -- "where is everything up to" means
    the live pipeline, not the history.
    """
    if stage is not None and stage not in ai_pipeline.STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stage '{stage}'. Valid: {', '.join(ai_pipeline.STAGES)}",
        )
    if awaiting is not None and awaiting not in ("staff", "client"):
        raise HTTPException(status_code=400, detail="awaiting must be 'staff' or 'client'")

    records = ai_pipeline.build_records(db, ctx.venue)

    if stage is not None:
        records = [r for r in records if r.stage == stage]
    else:
        records = [r for r in records if r.stage != "archived"]
    if awaiting is not None:
        records = [r for r in records if r.awaiting == awaiting]

    counts: dict[str, int] = {}
    for record in records:
        counts[record.stage] = counts.get(record.stage, 0) + 1

    grouped: dict[str, list] = {}
    for record in records:
        grouped.setdefault(record.stage, []).append(_pipeline_payload(record))

    return {
        "as_of": ctx.as_of_iso,
        "venue": ctx.venue.slug,
        "total": len(records),
        "counts": counts,
        "by_stage": grouped,
        "records": [_pipeline_payload(r) for r in records],
        "notes": {
            "replied": (
                "Never returned yet: it requires a logged staff reply, and staff reply "
                "from Gmail which Concierge does not see. Until the log-reply action "
                "exists, an answered enquiry still reads as 'enquiry' and 'awaiting: staff'."
            ),
            "beo_sent": (
                "Means the Event Order was issued, not that a client approved it -- "
                "only agreements can be signed in Concierge."
            ),
        },
    }


# --- 3.2 availability ---------------------------------------------------


@router.get("/availability")
def availability(
    date: dt.date | None = Query(default=None),
    date_from: dt.date | None = Query(default=None, alias="from"),
    date_to: dt.date | None = Query(default=None, alias="to"),
    space: str | None = Query(default=None),
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Everything touching each slot: confirmed, tentative, and every open
    enquiry. Time-aware, so a lunch and an evening in the same room are
    both listed and neither conceals the other."""
    if date is not None:
        start = end = date
    elif date_from is not None and date_to is not None:
        start, end = date_from, date_to
    else:
        raise HTTPException(
            status_code=400, detail="Provide either ?date= or both ?from= and ?to="
        )

    if end < start:
        raise HTTPException(status_code=400, detail="'to' must not be before 'from'")
    if (end - start).days + 1 > MAX_AVAILABILITY_DAYS:
        raise HTTPException(
            status_code=400, detail=f"Range too wide (max {MAX_AVAILABILITY_DAYS} days)"
        )

    space_row = _space_by_slug(db, ctx.venue, space)
    days = ai_availability.build_availability(
        db,
        ctx.venue,
        date_from=start,
        date_to=end,
        space_id=space_row.id if space_row else None,
    )
    return {"as_of": ctx.as_of_iso, "venue": ctx.venue.slug, "days": days}


# --- 3.1 booking lookup -------------------------------------------------


def _booking_payload(db: Session, booking: Booking) -> dict:
    agreement = next(
        (d for d in booking.documents if d.type == DocumentType.agreement and d.is_current), None
    )
    beo = next((d for d in booking.documents if d.type == DocumentType.beo and d.is_current), None)
    deposit = next(
        (
            i for i in booking.invoices
            if i.type == InvoiceType.deposit and i.status != InvoiceStatus.cancelled
        ),
        None,
    )
    wizard = booking.wizard_session
    snapshot = booking.migration_snapshot or {}

    return {
        "reference": booking.reference_code,
        "id": str(booking.id),
        "event_name": booking.event_name,
        "event_type": booking.event_type,
        "status": booking.status.value,
        "stage": ai_pipeline.compute_stage(booking),
        "event_date": booking.event_date.isoformat() if booking.event_date else None,
        "day_of_week": ai_availability.day_of_week(booking.event_date) if booking.event_date else None,
        "start_time": booking.start_time.strftime("%H:%M") if booking.start_time else None,
        "end_time": booking.end_time.strftime("%H:%M") if booking.end_time else None,
        "space": booking.space.name if booking.space else None,
        "linked_spaces": [c.space.name for c in booking.linked_bookings if c.space],
        "adults": booking.adult_count,
        "children": booking.child_count,
        "agreed_min_adults": booking.agreed_min_adults,
        "space_default_min_adults": booking.space.standard_min_adults if booking.space else None,
        "min_reduction_reason": (
            booking.agreed_min_reduction_reason.value
            if booking.agreed_min_reduction_reason
            else None
        ),
        # Not a field yet -- the per-booking override is parked, so the
        # space default is the only real figure. Reported as null rather
        # than echoing the default, which would look like a negotiated term.
        "agreed_min_food_spend": None,
        "space_default_min_food_spend": (
            str(booking.space.min_food_spend) if booking.space else None
        ),
        "pricing_locked_at": booking.pricing_locked_at.isoformat() if booking.pricing_locked_at else None,
        # Named for what it actually governs. The May 2026 cutover is the
        # pizza legacy price, not a general pricing regime.
        "pizza_pricing_basis": (
            "pre_may_2026"
            if booking.pricing_locked_at
            and booking.pricing_locked_at < policy.PIZZA_LEGACY_PRICING_CUTOVER_DATE
            else "current"
        ),
        "contact_name": booking.contact.name if booking.contact else None,
        # A list from day one even though Contact holds exactly one
        # address: the multi-address migration should not change this
        # response shape when it lands.
        "contact_emails": [booking.contact.email] if booking.contact else [],
        "contact_phone": booking.contact.phone if booking.contact else None,
        # Not a first-class field; only imported bookings carry one.
        "company": snapshot.get("company"),
        "agreement_status": agreement.status.value if agreement else None,
        "agreement_signed_at": agreement.signed_at.isoformat() if agreement and agreement.signed_at else None,
        "agreement_is_legacy": bool(agreement.is_legacy) if agreement else None,
        "deposit_status": deposit.status.value if deposit else None,
        "deposit_amount": str(deposit.total) if deposit else None,
        "deposit_paid_at": deposit.paid_at.isoformat() if deposit and deposit.paid_at else None,
        "deposit_is_legacy": bool(deposit.is_legacy) if deposit else None,
        "wizard_status": wizard.status.value if wizard else None,
        # There is no wizard due date field; the link's expiry is the only
        # real deadline stored.
        "wizard_expires_at": wizard.expires_at.isoformat() if wizard and wizard.expires_at else None,
        "wizard_submitted_at": wizard.submitted_at.isoformat() if wizard and wizard.submitted_at else None,
        "beo_status": beo.status.value if beo else None,
        "beo_version": beo.version if beo else None,
        "outside_cake_permitted": booking.outside_cake_permitted,
        "migration_source": booking.migration_source,
        "migration_external_ref": booking.migration_external_ref,
        "flags": [
            e.new_value
            for e in booking.events
            if e.event_type == "enquiry_flagged" and e.new_value
        ],
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
        "updated_at": booking.updated_at.isoformat() if booking.updated_at else None,
    }


@router.get("/bookings")
def bookings(
    ref: str | None = Query(default=None),
    email: str | None = Query(default=None),
    date: dt.date | None = Query(default=None),
    space: str | None = Query(default=None),
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Booking lookup by reference, contact email, or date (+ optional
    space). The date form is the second, independent path the availability
    cross-check in section 10a.2 compares against."""
    if not any([ref, email, date]):
        raise HTTPException(status_code=400, detail="Provide one of ?ref=, ?email= or ?date=")

    space_row = _space_by_slug(db, ctx.venue, space)

    if date is not None:
        rows = ai_availability.bookings_on_date(
            db, ctx.venue, on=date, space_id=space_row.id if space_row else None
        )
        ids = [b.id for b in rows]
    else:
        stmt = (
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(Space.venue_id == ctx.venue.id)
        )
        if ref:
            stmt = stmt.where(Booking.reference_code == ref.strip())
        if email:
            stmt = stmt.join(Contact, Booking.contact_id == Contact.id).where(
                Contact.email.ilike(email.strip())
            )
        ids = list(db.scalars(stmt.with_only_columns(Booking.id)).all())

    if not ids:
        return {"as_of": ctx.as_of_iso, "venue": ctx.venue.slug, "count": 0, "bookings": []}

    loaded = list(
        db.scalars(
            select(Booking)
            .where(Booking.id.in_(ids))
            .options(
                selectinload(Booking.documents),
                selectinload(Booking.invoices),
                selectinload(Booking.events),
                selectinload(Booking.space),
                selectinload(Booking.contact),
                selectinload(Booking.wizard_session),
                selectinload(Booking.linked_bookings).selectinload(Booking.space),
            )
            .order_by(Booking.event_date, Booking.start_time)
        ).all()
    )

    return {
        "as_of": ctx.as_of_iso,
        "venue": ctx.venue.slug,
        "count": len(loaded),
        "bookings": [_booking_payload(db, b) for b in loaded],
    }


# --- 3.3 catalogue ------------------------------------------------------


@router.get("/catalogue")
def catalogue(
    as_of: dt.date | None = Query(default=None),
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Menu items with the price that actually applies.

    Without `as_of` this is today's catalogue. With it, prices resolve as
    they would for a booking whose pricing was locked on that date -- which
    for pizzas before the May 2026 cutover means the legacy price.

    A null price is meaningful, not missing data: the item is legacy-priced
    but no legacy price was ever defined for it (Vegetarian Pizza is the
    real case). It must be surfaced as unknown, never guessed.
    """
    items = list(db.scalars(select(MenuItem).order_by(MenuItem.category, MenuItem.name)).all())

    payload = []
    for item in items:
        price = (
            catalogue_service.resolve_price_as_of(item, as_of)
            if as_of is not None
            else item.current_price
        )
        payload.append(
            {
                "id": str(item.id),
                "name": item.name,
                "category": item.category.value,
                "price": str(price) if price is not None else None,
                "price_unknown_reason": (
                    "legacy-priced booking, but no legacy price was ever defined for this item"
                    if price is None
                    else None
                ),
                "current_price": str(item.current_price),
                "legacy_price": str(item.legacy_price) if item.legacy_price is not None else None,
                "is_active": item.is_active,
                # NULL means "not yet confirmed against the website", which is
                # different from [] meaning "confirmed to carry no marker".
                # The AI must not read null as "no dietary markers".
                "dietary_markers": item.dietary_markers,
                "contains_peanuts": item.contains_peanuts,
            }
        )

    return {
        "as_of": ctx.as_of_iso,
        "pricing_as_of": as_of.isoformat() if as_of else None,
        "venue": ctx.venue.slug,
        "count": len(payload),
        "items": payload,
        "notes": {
            "serving_sizes": (
                "Not stored. No item carries a guest count, so a platter cannot be "
                "described as feeding a fixed number of people from this data."
            ),
            "dietary_markers": (
                "null means unconfirmed against the website, not 'none'. Only an "
                "empty list means confirmed to carry no marker."
            ),
        },
    }


# --- 3.4 / 3.5 per-booking detail ---------------------------------------


def _ai_booking_or_404(db: Session, ctx: AiContext, booking_id: uuid.UUID) -> Booking:
    """Venue-scoped lookup. A booking outside this credential's venue is
    reported as absent rather than forbidden -- the credential should not
    be able to confirm that it exists (brief section 7)."""
    booking = db.scalar(
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(Booking.id == booking_id, Space.venue_id == ctx.venue.id)
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.get("/bookings/{booking_id}/documents")
def booking_documents(
    booking_id: uuid.UUID,
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Whether documents exist and what state they are in -- deliberately
    NOT their content and never the PDF (brief sections 3.4 and 6). The AI
    does not need to read a contract to check that one is signed."""
    booking = _ai_booking_or_404(db, ctx, booking_id)
    return {
        "as_of": ctx.as_of_iso,
        "reference": booking.reference_code,
        "documents": [
            {
                "id": str(d.id),
                "type": d.type.value,
                "version": d.version,
                "is_current": d.is_current,
                "status": d.status.value,
                "viewed_at": d.viewed_at.isoformat() if d.viewed_at else None,
                "signed_at": d.signed_at.isoformat() if d.signed_at else None,
                "signer_name": d.signer_name,
                "is_legacy": d.is_legacy,
                "legacy_source_ref": d.legacy_source_ref,
            }
            for d in sorted(booking.documents, key=lambda d: (d.type.value, d.version))
        ],
    }


@router.get("/bookings/{booking_id}/invoices")
def booking_invoices(
    booking_id: uuid.UUID,
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """Invoice state and amounts.

    Payment rows carry amount, method and payer, but NEVER Payment.reference
    -- for a card payment that field holds the Stripe payment_intent id, and
    no AI endpoint returns a Stripe identifier (brief section 3.6). Bank and
    card details are likewise absent because they are not stored on these
    rows at all.
    """
    booking = _ai_booking_or_404(db, ctx, booking_id)
    return {
        "as_of": ctx.as_of_iso,
        "reference": booking.reference_code,
        "invoices": [
            {
                "id": str(i.id),
                "invoice_number": i.invoice_number,
                "type": i.type.value,
                "status": i.status.value,
                "total": str(i.total),
                "due_date": i.due_date.isoformat() if i.due_date else None,
                "paid_at": i.paid_at.isoformat() if i.paid_at else None,
                "viewed_at": i.viewed_at.isoformat() if i.viewed_at else None,
                "is_legacy": i.is_legacy,
                "payments": [
                    {
                        "amount": str(p.amount),
                        "method": p.method.value,
                        "payer_name": p.payer_name,
                        "received_at": p.received_at.isoformat() if p.received_at else None,
                    }
                    for p in sorted(i.payments, key=lambda p: p.received_at)
                ],
            }
            for i in sorted(booking.invoices, key=lambda i: i.created_at)
        ],
    }


@router.get("/bookings/{booking_id}/events")
def booking_events(
    booking_id: uuid.UUID,
    limit: int = Query(default=200, le=1000),
    ctx: AiContext = Depends(require_ai),
    db: Session = Depends(get_db),
):
    """The existing BookingEvent history, so "when did this change and who
    changed it" is answered from the record rather than guessed."""
    booking = _ai_booking_or_404(db, ctx, booking_id)
    events = sorted(booking.events, key=lambda e: e.created_at)[-limit:]
    return {
        "as_of": ctx.as_of_iso,
        "reference": booking.reference_code,
        "count": len(events),
        "events": [
            {
                "at": e.created_at.isoformat() if e.created_at else None,
                "event_type": e.event_type,
                "field_name": e.field_name,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "actor": e.actor,
            }
            for e in events
        ],
    }
