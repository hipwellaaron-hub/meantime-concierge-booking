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
import logging
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
from app.services import notifications
from app.services.document_generation import build_av_block, build_vendor_snapshot, generate_beo_content
from app.utils import truncate

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class WizardGenerationResult:
    document: Document
    invoice: Invoice
    outstanding_items: list[str]
    is_clean: bool


def build_food_line_items(
    db: Session, booking: Booking, food_response: dict | None, extras_response: dict | None = None
) -> tuple[list[dict], list[str]]:
    """Resolves each selected item's real price (current vs. legacy) --
    same resolution logic already proven in app.services.wizard.save_food_step.
    Items with no resolvable price are excluded from the returned line
    items (never guessed) and named in outstanding_items instead.

    Lookups use get_by_id_any, not get_by_id: these are already-stored
    selections, and an item retired since the client picked it must keep
    its name and quoted price -- a deleted row (id gone entirely) is the
    only genuinely-unresolvable case.

    Every line carries a "category" so the Event Order can group into
    Platters / Pizzas / Sides / Desserts without name matching.

    An in-house cake chosen in the extras step becomes a priced dessert
    line here -- THE fix for the undercharge where the cake only ever
    appeared as a Special Notes sentence and never reached the food total
    or the invoice. This list feeds both the BEO and
    invoicing.create_invoice (see generate_beo_and_invoice), so pricing
    the cake here fixes the invoice with no invoice-side change.
    """
    line_items: list[dict] = []
    outstanding: list[str] = []
    food_response = food_response or {}

    for key in ("platters", "pizzas", "sides", "desserts"):
        for entry in food_response.get(key, []):
            menu_item = catalogue.get_by_id_any(db, uuid.UUID(entry["menu_item_id"]))
            if menu_item is None:
                outstanding.append(
                    "A previously selected item is no longer available and needs confirming with the client"
                )
                continue
            price = catalogue.resolve_price(menu_item, booking)
            if price is None:
                outstanding.append(
                    f"{menu_item.name} needs price confirmation (legacy booking, no pre-cutover price on record)"
                )
                continue
            line_items.append(
                {
                    "description": menu_item.name,
                    "quantity": entry["quantity"],
                    "unit_price": str(price),
                    "category": menu_item.category.value,
                }
            )

    cake = ((extras_response or {}).get("cake_choice") or {})
    if cake.get("type") == "in_house":
        cake_item = catalogue.get_by_id_any(db, uuid.UUID(cake["menu_item_id"])) if cake.get("menu_item_id") else None
        if cake_item is None:
            outstanding.append("In-house cake chosen but no priced cake on record -- confirm the selection with the client")
        else:
            cake_price = catalogue.resolve_price(cake_item, booking)
            if cake_price is None:
                outstanding.append(f"{cake_item.name} needs price confirmation before it can be charged")
            else:
                line_items.append(
                    {
                        "description": f"{cake_item.name} — celebration cake",
                        "quantity": 1,
                        "unit_price": str(cake_price),
                        "category": "dessert",
                    }
                )

    return line_items, outstanding


def build_catering_order_text(booking: Booking, food_response: dict | None) -> str | None:
    from app.services.document_generation import format_time_12h

    if not food_response:
        return None
    platter_count = sum(item["quantity"] for item in food_response.get("platters", []))
    pizza_count = sum(item["quantity"] for item in food_response.get("pizzas", []))
    lines = ["Shared platters and pizzas, cocktail style."]
    if booking.food_service_time is not None:
        lines.append(f"Food service from {format_time_12h(booking.food_service_time)}.")
    else:
        lines.append("Food service time not yet confirmed.")
    lines.append(f"{platter_count} platter(s) and {pizza_count} pizza(s) — see Food Order.")
    if food_response.get("sides"):
        lines.append("Sides served later in the evening.")
    return "\n".join(lines)


# The bar inclusion checkboxes the wizard's beverage step offers, and the
# order they read naturally in a sentence. Beverage packages are
# discontinued and deliberately have no representation anywhere here.
BAR_INCLUSION_LABELS = {
    "beer": "beer",
    "wine": "wine",
    "soft_drinks": "soft drinks",
    "standard_spirits": "standard spirits",
}


def _format_money(value) -> str:
    """"$2,000" -- thousands-separated, no trailing cents when whole."""
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def _human_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _inclusions_text(inclusions) -> str | None:
    """Handles both shapes: the structured checkbox list the beverage step
    now saves, and the free-text string older sessions stored (the source
    of the garbled "Everything Bear wine cocktails spirits" output)."""
    if not inclusions:
        return None
    if isinstance(inclusions, list):
        labels = [BAR_INCLUSION_LABELS.get(key, key.replace("_", " ")) for key in inclusions]
        return _human_join(labels)
    return str(inclusions).strip() or None


def build_bar_structure_text(beverage_response: dict | None) -> str | None:
    """Newline-separated bullet lines (the template renders each line as a
    bullet). House position: hybrid means a dollar-limit tab that switches
    to guest-pays at the cap -- never a category split."""
    if not beverage_response:
        return None
    structure = beverage_response.get("bar_structure")
    limit = beverage_response.get("bar_limit")
    covering = _inclusions_text(beverage_response.get("bar_inclusions"))

    if structure == "cash_bar":
        return "Cash bar — guests purchase all drinks directly."

    if structure in ("bar_tab", "hybrid"):
        cap = _format_money(limit) if limit is not None else None
        first = f"Bar tab to {cap}" if cap else "Bar tab"
        if covering:
            first += f", covering {covering}"
        lines = [first + "."]
        lines.append("Cocktails and premium spirits purchased by guests.")
        if structure == "hybrid":
            lines.append("Once the cap is reached, the bar switches to guest-pays.")
        lines.append("Tracked live; host notified as the cap approaches.")
        note = beverage_response.get("bar_inclusions_note")
        if note:
            lines.append(note)
        return "\n".join(lines)
    return None


MUSIC_TYPE_LINES = {
    "own_playlist": "Client's own Spotify playlist — set to public, playlist name given to the team on the night (no links).",
    "dj": "DJ.",
    "musician": "Musician (venue-arranged).",
}


def build_music_text(music_response: dict | None) -> str | None:
    """The Music section only -- entertainment (slideshows, performers,
    games) is a separate section, see build_entertainment_text. House rule
    composed in for playlists: Spotify only, set to public, playlist NAME
    given to the team on the night -- never a link.

    Multi-select: a playlist before/after a DJ set is normal, so every
    selected type gets its line. Older sessions stored a single
    music_type; that still reads correctly."""
    if not music_response:
        return None
    selected = music_response.get("music_types") or (
        [music_response["music_type"]] if music_response.get("music_type") else []
    )
    lines = [MUSIC_TYPE_LINES.get(t, t) for t in selected]
    if music_response.get("notes"):
        lines.append(music_response["notes"])
    if music_response.get("bump_in_notes"):
        lines.append(f"Bump-in: {music_response['bump_in_notes']}")
    return "\n".join(lines) if lines else None


def build_entertainment_text(extras_response: dict | None, av_response: dict | None) -> str | None:
    """Everything that isn't music: slideshows, performers, games,
    activities. Sourced from the AV step's slideshow answer and the
    extras step's additional notes -- None (section omitted) when there's
    genuinely nothing, rather than an empty heading."""
    lines = []
    if (av_response or {}).get("video_slideshow"):
        lines.append("Photo/video slideshow on the venue screen (USB).")
    additional = (extras_response or {}).get("additional_notes")
    if additional:
        lines.append(additional)
    return "\n".join(lines) if lines else None


def build_special_notes(extras_response: dict | None, booking: Booking | None = None, vendors: list[dict] | None = None) -> str | None:
    """Newline-separated bullet lines. Dietaries, accessibility and
    decorations are NOT here anymore -- each is its own first-class BEO
    field now (a declared allergy buried in a notes blob is how it went
    missing) -- but the guest split and vendors without a bump-in time do
    belong here, per the reference document."""
    lines = []

    if booking is not None and (booking.adult_count or booking.child_count):
        lines.append(f"{booking.adult_count} adults, {booking.child_count} kids")

    cake = (extras_response or {}).get("cake_choice") or {}
    if cake.get("type") == "in_house":
        lines.append("Cake: in-house selection — see Desserts in the Food Order.")
    elif cake.get("type") == "outside":
        note = cake.get("notes") or ""
        lines.append(f"Cake: client bringing their own (gluten-free only, stays on the cake table, not in the kitchen). {note}".strip())

    # A vendor with a bump-in time renders on the Event Timeline instead
    # (a time the floor team acts on); one without still needs its name
    # and contact visible somewhere -- that somewhere is here.
    from app.services.document_generation import vendor_type_label

    for vendor in vendors or []:
        if not vendor.get("bump_in_display"):
            contact = f" ({vendor['contact_number']})" if vendor.get("contact_number") else ""
            lines.append(f"Vendor: {vendor_type_label(vendor['vendor_type'])} — {vendor['name']}{contact}")

    # The review step's "anything else we should know?" escape hatch.
    if (extras_response or {}).get("final_notes"):
        lines.append(f"Client note: {extras_response['final_notes']}")

    # layout_notes and additional_notes are deliberately absent: layout is
    # its own BEO section, and additional notes feed Entertainment.
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
    food_line_items, _ = build_food_line_items(db, booking, session.food_response, session.extras_response)
    deposit_paid = invoicing.get_deposit_paid(db, booking)
    return generate_beo_content(booking, food_line_items, **_session_content_kwargs(db, session, deposit_paid))


def get_prior_beo_internal_notes(db: Session, booking: Booking) -> str | None:
    """Internal notes are staff-entered on the BEO edit screen, not
    wizard-captured -- so a Regenerate must carry them forward from the
    current BEO rather than silently wiping the kitchen's brief."""
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    if current is None:
        return None
    return (current.content or {}).get("internal_notes")


def _session_content_kwargs(db: Session, session: WizardSession, deposit_paid) -> dict:
    """The full keyword set for generate_beo_content, derived from a
    wizard session -- one place, used by both the submission path and the
    staff Regenerate path so the two can never diverge."""
    booking = session.booking
    extras = session.extras_response or {}
    vendors = build_vendor_snapshot(booking.vendors)
    total_paid = sum(
        (invoicing.get_total_paid(db, invoice.id) for invoice in booking.invoices),
        Decimal("0.00"),
    )
    return {
        "catering_order_and_service_style": build_catering_order_text(booking, session.food_response),
        "bar_structure": build_bar_structure_text(session.beverage_response),
        "room_layout_notes": extras.get("layout_notes"),
        "music": build_music_text(session.music_response),
        "entertainment": build_entertainment_text(session.extras_response, session.av_response),
        "dietaries": extras.get("dietary_requirements"),
        "accessibility": extras.get("accessibility_needs"),
        "decorations": extras.get("decorations_notes"),
        "status_text": _build_status_text(db, booking),
        "special_notes_extra": build_special_notes(session.extras_response, booking, vendors),
        "av": build_av_block(booking, session.av_response),
        "vendors": vendors,
        "internal_notes": get_prior_beo_internal_notes(db, booking),
        "deposit_paid": deposit_paid,
        "total_paid": total_paid,
    }


def _build_status_text(db: Session, booking: Booking) -> str:
    """The reference document's Status section, e.g. "Deposit paid,
    agreement signed. Awaiting Event Order approval and final invoice
    payment." Derived from real state, staff-overridable on the edit
    screen."""
    from app.services.booking import has_paid_deposit, has_signed_agreement

    done = []
    if has_paid_deposit(db, booking):
        done.append("Deposit paid")
    if has_signed_agreement(db, booking):
        done.append("agreement signed")
    prefix = (", ".join(done) + ". ") if done else ""
    return f"{prefix}Awaiting Event Order approval and final invoice payment."


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
        "vendors_response": "Suppliers & Vendors",
        "extras_response": "Extras",
    }
    if booking.space.name == "The Loft":
        # The AV step only exists for Loft bookings -- flagging a skipped
        # AV step on a Mezzanine booking would be flagging a step the
        # client was never shown.
        step_labels["av_response"] = "AV & Screen"
    for field, label in step_labels.items():
        if getattr(session, field) is None:
            outstanding_items.append(f"{label} step was not completed")

    food_line_items, food_outstanding = build_food_line_items(db, booking, session.food_response, session.extras_response)
    outstanding_items += food_outstanding

    deposit_paid = invoicing.get_deposit_paid(db, booking)

    if session.has_hard_escalation:
        outstanding_items.append("Accessibility need raised against a non-accessible space -- requires Aaron's review")

    beo_content = generate_beo_content(booking, food_line_items, **_session_content_kwargs(db, session, deposit_paid))

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

    _notify_wizard_submission(db, booking, outstanding_items=outstanding_items, actor=actor)

    return WizardGenerationResult(document=document, invoice=invoice, outstanding_items=outstanding_items, is_clean=is_clean)


def _notify_wizard_submission(db: Session, booking: Booking, *, outstanding_items: list[str], actor: str) -> None:
    """Tells the venue a client has finished their wizard. Until this
    existed, "escalated to Aaron" wrote a line to the audit trail and
    stopped -- nothing actually reached him, so a submission could sit
    unseen inside the 14-day window where details stop being negotiable.

    Never raises, and never lets a failed send undo the submission: the
    client has completed their wizard and the BEO exists either way. A
    failure is written to the audit trail so it's visible in the app,
    which is also the record the dashboard's own BEO worklist relies on
    (that list is built from BEO drafts, not from this email, so it works
    whether or not the email ever sends)."""
    if not notifications.is_gmail_smtp_configured():
        logger.warning(
            "Wizard submission notification not sent for %s: Gmail SMTP is not configured", booking.reference_code
        )
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_notification_failed",
                new_value="Gmail SMTP is not configured",
                actor=actor,
            )
        )
        db.commit()
        return

    try:
        notifications.send_wizard_submission_email(
            booking, outstanding_items=outstanding_items, dashboard_base_url=settings.dashboard_base_url
        )
    except Exception as exc:  # noqa: BLE001 -- a mail problem must never lose a completed submission
        logger.exception("Wizard submission notification failed for %s", booking.reference_code)
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="wizard_notification_failed",
                new_value=truncate(str(exc), 500),
                actor=actor,
            )
        )
        db.commit()
        return

    db.add(BookingEvent(booking_id=booking.id, event_type="wizard_notification_sent", actor=actor))
    db.commit()
