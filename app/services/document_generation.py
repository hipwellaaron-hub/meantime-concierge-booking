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


def generate_beo_content(booking: Booking, food_order_line_items: list[dict] | None = None) -> dict:
    food_order_line_items = food_order_line_items or []
    food_total = compute_food_order_total(food_order_line_items)

    return {
        "event_timeline": {
            "event_date": booking.event_date.isoformat(),
            "start_time": _format_time(booking.start_time),
            "end_time": _format_time(booking.end_time),
            "notes": f"{REVIEW} add run-sheet detail beyond start/end time",
        },
        "catering_order_and_service_style": f"{REVIEW} add catering order and service style",
        "food_order": {
            "line_items": food_order_line_items,
            "note": None if food_order_line_items else f"{REVIEW} no food order captured yet",
        },
        "total_food_spend": {
            "total": str(food_total) if food_total is not None else None,
            "deposit_paid": None,
            "balance_due": None,
            "note": (
                f"{REVIEW} add a food order above to compute the total"
                if food_total is None
                else f"{REVIEW} deposit paid / balance due aren't derivable yet -- "
                "payments aren't tracked in Concierge until Phase 3"
            ),
        },
        "bar_structure": f"{REVIEW} add bar structure",
        "room_layout_notes": f"{REVIEW} add room layout notes",
        "music_entertainment": f"{REVIEW} add music/entertainment detail",
        "special_notes": booking.notes or "",
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
        "event_date": booking.event_date.isoformat(),
        "start_time": _format_time(booking.start_time),
        "end_time": _format_time(booking.end_time),
        "adult_count": booking.adult_count,
        "child_count": booking.child_count,
        "min_food_spend": str(space.min_food_spend),
        "standard_min_adults": space.standard_min_adults,
        "deposit_required": str(STANDARD_DEPOSIT),
        "terms_text": f"{REVIEW} paste current cancellation/payment terms from the Handover doc",
    }
