"""Auto-populates document content from structured booking data, and
nothing else -- anywhere the data doesn't exist yet (no Handover doc was
available at build time; payments don't exist until Phase 3), this emits
an explicit [REVIEW] marker rather than guessing. Both silent guessing and
hard blocking are wrong; this is the middle path.
"""

from decimal import Decimal, InvalidOperation

from app.models import Booking
from app.services.policy import STANDARD_DEPOSIT

REVIEW = "[REVIEW]"


def _format_time(value) -> str:
    # Enquiry-stage bookings may not have a pinned-down start/end time yet
    # (see proposed_time_slot on Booking) -- guessing one would be worse
    # than flagging it.
    return value.strftime("%H:%M") if value is not None else f"{REVIEW} time not yet finalized"


def _format_date(value) -> str:
    # Same reasoning as _format_time: an enquiry can arrive with no date
    # locked in yet (see the missing_event_date flag in
    # app.services.enquiry_classification) -- a document generated before
    # that's resolved must say so, not crash or guess one.
    return value.isoformat() if value is not None else f"{REVIEW} event date not yet confirmed"


def compute_food_order_total(line_items: list[dict]) -> Decimal | None:
    if not line_items:
        return None
    try:
        return sum(
            (Decimal(str(item["quantity"])) * Decimal(str(item["unit_price"])) for item in line_items),
            Decimal("0.00"),
        )
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"malformed food order line item: {exc}") from exc


def generate_beo_content(
    booking: Booking,
    food_order_line_items: list[dict] | None = None,
    *,
    catering_order_and_service_style: str | None = None,
    bar_structure: str | None = None,
    room_layout_notes: str | None = None,
    music_entertainment: str | None = None,
    special_notes_extra: str | None = None,
    deposit_paid: Decimal | None = None,
) -> dict:
    """Every new keyword-only param defaults to None, which preserves the
    exact original [REVIEW]-placeholder output byte for byte -- this
    function still has no opinion on what's genuinely missing vs. real
    captured data with nothing outstanding; that distinction is the
    caller's job (see app.services.wizard_generation, which is the only
    caller that ever passes these)."""
    food_order_line_items = food_order_line_items or []
    food_total = compute_food_order_total(food_order_line_items)

    if food_total is None:
        total_food_spend_note = f"{REVIEW} add a food order above to compute the total"
        balance_due = None
    elif deposit_paid is None:
        total_food_spend_note = (
            f"{REVIEW} deposit paid / balance due aren't derivable yet -- "
            "payments aren't tracked in Concierge until Phase 3"
        )
        balance_due = None
    else:
        total_food_spend_note = None
        balance_due = food_total - deposit_paid

    return {
        "event_timeline": {
            "event_date": _format_date(booking.event_date),
            "start_time": _format_time(booking.start_time),
            "end_time": _format_time(booking.end_time),
            "notes": f"{REVIEW} add run-sheet detail beyond start/end time",
        },
        "catering_order_and_service_style": catering_order_and_service_style or f"{REVIEW} add catering order and service style",
        "food_order": {
            "line_items": food_order_line_items,
            "note": None if food_order_line_items else f"{REVIEW} no food order captured yet",
        },
        "total_food_spend": {
            "total": str(food_total) if food_total is not None else None,
            "deposit_paid": str(deposit_paid) if deposit_paid is not None else None,
            "balance_due": str(balance_due) if balance_due is not None else None,
            "note": total_food_spend_note,
        },
        "bar_structure": bar_structure or f"{REVIEW} add bar structure",
        "room_layout_notes": room_layout_notes if room_layout_notes is not None else f"{REVIEW} add room layout notes",
        "music_entertainment": music_entertainment or f"{REVIEW} add music/entertainment detail",
        "special_notes": special_notes_extra if special_notes_extra is not None else (booking.notes or ""),
        "status": booking.status.value,
        "_reference": {
            "reference_code": booking.reference_code,
            "event_name": booking.event_name,
            "space_name": booking.space.name,
            "adult_count": booking.adult_count,
            "child_count": booking.child_count,
        },
    }


def generate_agreement_content(booking: Booking) -> dict:
    space = booking.space
    return {
        "venue": space.venue.name,
        "space_name": space.name,
        "event_name": booking.event_name,
        "event_date": _format_date(booking.event_date),
        "start_time": _format_time(booking.start_time),
        "end_time": _format_time(booking.end_time),
        "adult_count": booking.adult_count,
        "child_count": booking.child_count,
        "min_food_spend": str(space.min_food_spend),
        "standard_min_adults": space.standard_min_adults,
        "deposit_required": str(STANDARD_DEPOSIT),
        "terms_text": f"{REVIEW} paste current cancellation/payment terms from the Handover doc",
    }
