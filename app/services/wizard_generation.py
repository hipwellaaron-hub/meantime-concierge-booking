"""Orchestrates BEO + final invoice generation from a submitted Guided
Booking Wizard session. Kept separate from app.services.wizard (step
management) and app.services.document_generation (pure content
formatting) -- this module's only job is composing real captured data
into both, deciding whether the result is clean enough to route
automatically, and recording what happened.

The [REVIEW]-marker convention (app.services.document_generation) stays
unchanged for non-wizard callers. For wizard-sourced generation, the
Master Policy doc (§7) is stricter: "outstanding items are raised
separately so the floor and kitchen team receive a clean document" --
nothing here embeds review commentary inside the BEO's own content
fields. Anything outstanding is collected into a separate list instead.
"""

import dataclasses
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, BookingEvent, Document, Invoice
from app.models.document import DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.wizard_session import WizardSession
from app.services import catalogue
from app.services import documents as documents_service
from app.services import invoicing
from app.services.document_generation import generate_beo_content


@dataclasses.dataclass
class WizardGenerationResult:
    document: Document
    invoice: Invoice
    outstanding_items: list[str]
    is_clean: bool


def build_food_line_items(db: Session, booking: Booking, food_response: dict | None) -> tuple[list[dict], list[str]]:
    """Resolves each selected item's real price (current vs. legacy) --
    same resolution logic already proven in app.services.wizard.save_food_step.
    Items with no resolvable price are excluded from the returned line
    items (never guessed) and named in outstanding_items instead."""
    if not food_response:
        return [], []

    line_items: list[dict] = []
    outstanding: list[str] = []

    for entry in food_response.get("platters", []) + food_response.get("pizzas", []):
        menu_item = catalogue.get_by_id(db, uuid.UUID(entry["menu_item_id"]))
        if menu_item is None:
            outstanding.append("A previously selected item is no longer available and needs confirming with the client")
            continue
        price = catalogue.resolve_price(menu_item, booking)
        if price is None:
            outstanding.append(f"{menu_item.name} needs price confirmation (legacy booking, no pre-cutover price on record)")
            continue
        line_items.append({"description": menu_item.name, "quantity": entry["quantity"], "unit_price": str(price)})

    return line_items, outstanding


def build_catering_order_text(booking: Booking, food_response: dict | None) -> str | None:
    if not food_response:
        return None
    platter_count = sum(item["quantity"] for item in food_response.get("platters", []))
    pizza_count = sum(item["quantity"] for item in food_response.get("pizzas", []))
    time_text = (
        f"Food service from {booking.food_service_time.strftime('%H:%M')}."
        if booking.food_service_time is not None
        else "Food service time not yet confirmed."
    )
    return f"{time_text} {platter_count} platter(s) and {pizza_count} pizza(s) ordered -- see Food Order below."


def build_bar_structure_text(beverage_response: dict | None) -> str | None:
    if not beverage_response:
        return None
    structure = beverage_response.get("bar_structure")
    limit = beverage_response.get("bar_limit")
    inclusions = beverage_response.get("bar_inclusions")

    if structure == "cash_bar":
        return "Cash bar -- guests purchase their own drinks."
    if structure == "bar_tab":
        text = f"Bar tab, capped at ${limit}."
        return f"{text} Includes: {inclusions}." if inclusions else text
    if structure == "hybrid":
        return f"Hybrid: bar tab up to ${limit}, switching to guest-pays after the cap."
    return None


def build_music_text(music_response: dict | None) -> str | None:
    if not music_response:
        return None
    label = "Client's own playlist" if music_response.get("music_type") == "own_playlist" else "DJ"
    parts = [label]
    if music_response.get("notes"):
        parts.append(music_response["notes"])
    if music_response.get("bump_in_notes"):
        parts.append(f"Bump-in: {music_response['bump_in_notes']}")
    return ". ".join(parts) + "."


def build_special_notes(extras_response: dict | None) -> str | None:
    if not extras_response:
        return None
    lines = []

    cake = extras_response.get("cake_choice") or {}
    if cake.get("type") == "in_house":
        lines.append("Cake: in-house selection (see Food Order / catalogue reference).")
    elif cake.get("type") == "outside":
        note = cake.get("notes") or ""
        lines.append(f"Cake: client bringing their own (gluten-free only, stays on the cake table, not in the kitchen). {note}".strip())

    if extras_response.get("dietary_requirements"):
        lines.append(f"Dietary requirements: {extras_response['dietary_requirements']}")
    if extras_response.get("accessibility_needs"):
        lines.append(f"Accessibility: {extras_response['accessibility_needs']}")
    if extras_response.get("decorations_notes"):
        lines.append(f"Decorations: {extras_response['decorations_notes']}")
    if extras_response.get("layout_notes"):
        lines.append(f"Layout: {extras_response['layout_notes']}")
    if extras_response.get("additional_notes"):
        lines.append(f"Additional notes: {extras_response['additional_notes']}")

    return "\n".join(lines) if lines else ""


def build_beo_content_for_session(db: Session, session: WizardSession) -> dict:
    """Real BEO content built from a wizard session's captured answers --
    the same real-data path used at automatic wizard-submission time (see
    generate_beo_and_invoice below). Also callable directly for a manual
    Regenerate from the staff dashboard, so a completed wizard's real
    answers are never silently discarded in favour of blank [REVIEW]
    placeholders just because staff triggered generation by hand rather
    than the client submitting. Ignores per-item pricing gaps (unlike
    generate_beo_and_invoice, this doesn't need an outstanding_items list
    -- it isn't deciding whether to auto-route anything)."""
    booking = session.booking
    food_line_items, _ = build_food_line_items(db, booking, session.food_response)
    deposit_paid = invoicing.get_deposit_paid(db, booking)
    return generate_beo_content(
        booking,
        food_line_items,
        catering_order_and_service_style=build_catering_order_text(booking, session.food_response),
        bar_structure=build_bar_structure_text(session.beverage_response),
        room_layout_notes=(session.extras_response or {}).get("layout_notes") if session.extras_response else None,
        music_entertainment=build_music_text(session.music_response),
        special_notes_extra=build_special_notes(session.extras_response),
        deposit_paid=deposit_paid,
    )


def generate_beo_and_invoice(db: Session, session: WizardSession, *, actor: str) -> WizardGenerationResult:
    booking = session.booking
    outstanding_items: list[str] = []

    # Completeness check first: nothing upstream stops a client POSTing
    # /review having skipped a step -- submit_review only checks the
    # session isn't already submitted. An incomplete submission must
    # never read as clean.
    step_labels = {
        "food_response": "Food",
        "beverage_response": "Beverage",
        "music_response": "Music",
        "extras_response": "Extras",
    }
    for field, label in step_labels.items():
        if getattr(session, field) is None:
            outstanding_items.append(f"{label} step was not completed")

    food_line_items, food_outstanding = build_food_line_items(db, booking, session.food_response)
    outstanding_items += food_outstanding

    deposit_paid = invoicing.get_deposit_paid(db, booking)

    if session.has_hard_escalation:
        outstanding_items.append("Accessibility need raised against a non-accessible space -- requires Aaron's review")

    beo_content = generate_beo_content(
        booking,
        food_line_items,
        catering_order_and_service_style=build_catering_order_text(booking, session.food_response),
        bar_structure=build_bar_structure_text(session.beverage_response),
        room_layout_notes=(session.extras_response or {}).get("layout_notes") if session.extras_response else None,
        music_entertainment=build_music_text(session.music_response),
        special_notes_extra=build_special_notes(session.extras_response),
        deposit_paid=deposit_paid,
    )

    document = documents_service.create_new_version(db, booking, DocumentType.beo, beo_content, actor=actor)

    # A staff-created manual final invoice (app.services.invoicing.
    # create_final_invoice) may already exist for this booking if the
    # client is completing the wizard after staff already invoiced them
    # by hand. Reusing that invoice rather than creating a second one
    # avoids a double-billed client; the mismatch is still surfaced via
    # outstanding_items so it always escalates for a human to reconcile,
    # never silently accepted as "clean".
    if invoicing.has_active_final_invoice(db, booking):
        outstanding_items.append("A final invoice already exists for this booking -- not creating a duplicate")
        invoice = db.execute(
            select(Invoice).where(
                Invoice.booking_id == booking.id,
                Invoice.type == InvoiceType.final,
                Invoice.status != InvoiceStatus.cancelled,
            )
        ).scalars().first()
    else:
        credit_line_items = (
            [{"description": "Less: deposit credited", "quantity": 1, "unit_price": str(-deposit_paid)}]
            if deposit_paid > 0
            else []
        )
        invoice = invoicing.create_invoice(
            db,
            booking,
            InvoiceType.final,
            food_line_items,
            due_date=booking.event_date,
            actor=actor,
            credit_line_items=credit_line_items,
        )

    is_clean = not outstanding_items

    if is_clean:
        db.add(BookingEvent(booking_id=booking.id, event_type="wizard_generation_clean", actor=actor))
    else:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_generation_needs_review",
                new_value="; ".join(outstanding_items),
                actor=actor,
            )
        )

    # Routing: gated behind config flags the user explicitly owns (see
    # app/config.py). A dirty submission ALWAYS escalates regardless of
    # either flag -- auto-routing only ever applies to a clean submission.
    if is_clean and settings.wizard_beo_auto_finalize:
        document = documents_service.mark_sent(db, document, actor=actor)
    if is_clean and settings.wizard_invoice_auto_send:
        invoice = invoicing.mark_sent(db, invoice, actor=actor)
    if not is_clean:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_escalated_to_aaron",
                new_value="draft BEO and invoice prepared, awaiting manual review and send",
                actor=actor,
            )
        )

    db.commit()
    db.refresh(document)
    db.refresh(invoice)

    return WizardGenerationResult(document=document, invoice=invoice, outstanding_items=outstanding_items, is_clean=is_clean)
