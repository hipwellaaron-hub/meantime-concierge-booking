"""Auto-populates document content from structured booking data, and
nothing else -- anywhere the data doesn't exist yet (no Handover doc was
available at build time; payments don't exist until Phase 3), this emits
an explicit [REVIEW] marker rather than guessing. Both silent guessing and
hard blocking are wrong; this is the middle path.
"""

from decimal import Decimal, InvalidOperation

from app.models import Booking
from app.services import policy
from app.services.enquiry_classification import looks_like_18th
from app.services.policy import STANDARD_DEPOSIT

REVIEW = "[REVIEW]"

# --- Master Policy v1.3 §3: client-facing contract terms --------------------
# Sourced verbatim from the Master Policy doc and cross-checked against a
# real signed contract. Shared clauses reference policy.py's own constants
# (never a second hardcoded copy of a figure) EXCEPT the Minimum Spend
# clause below, which Aaron was explicit must be two fully separate
# literal strings, not templated across spaces: "The one word difference
# is exactly the kind of drift that produced the errors in §3.4."

_DEPOSITS_CLAUSE = (
    f"A non-refundable deposit of ${STANDARD_DEPOSIT:.0f} is payable within 7 days of making your booking. "
    "The deposit will secure your date and event location. Meantime cannot guarantee any booking without a "
    "deposit."
)

_CREDIT_CARD_CLAUSE = "A valid credit card is required to secure your booking."

_BOOKING_AGREEMENT_CLAUSE = (
    "A signed completed Event Order is required no less than 7 days out from the event to ensure all details "
    "of your event are correct prior to commencement."
)

_DECORATIONS_CLAUSE = (
    "You are welcome to theme your event as you wish. No confetti or glitter. The hirer is required to ensure "
    "the room is left clean and tidy, or cleaning charges may be incurred. The hirer is liable for repair, "
    "damage or replacement of equipment caused by negligence of the hirer or the hirer's representatives."
)

_CREDIT_CARD_SURCHARGE_CLAUSE = (
    f"Credit card surcharges are applicable to all credit card payments. Surcharge rates are standard "
    f"{policy.CARD_SURCHARGE_RATE * 100:.1f}% across Mastercard, Visa, AMEX and Diners Club."
)

_CANCELLATION_POLICY_CLAUSE = (
    "Cancellation of your event must be notified in writing to the venue. If your event is cancelled 1 month "
    "or less prior to your event, the deposit is retained and you will be charged a "
    f"${policy.CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD:.0f} per head cancellation fee to the numbers stated on "
    "the booking agreement. If your event is cancelled with more than 1 month's notice, only the deposit is "
    "forfeited."
)

_PUBLIC_HOLIDAYS_CLAUSE = (
    f"All events booked on a public holiday will incur a {policy.PUBLIC_HOLIDAY_SURCHARGE_RATE * 100:.0f}% "
    "surcharge to cover penalty rates of our staff."
)

_TRADING_HOURS_CLAUSE = (
    "Meantime Hamilton operates 12pm to 9pm Wednesday and Thursday, 12pm to 12am Friday, 11am to 12am "
    "Saturday, and 11am to 9pm Sunday."
)

# Minimum Spend -- deliberately NOT derived from Space.min_food_spend.
# Update these by hand if the Master Policy doc's figures ever change.
_LOFT_MINIMUM_SPEND_CLAUSE = (
    "There is a $1,000 minimum spend required for your event across food. Your deposit will make up part of "
    "this total, and be deducted from your final invoice."
)
_MEZZANINE_MINIMUM_SPEND_CLAUSE = (
    "There is a $500 minimum spend required for your event across food. Your deposit will make up this "
    "total, and be deducted from your final invoice."
)

# 18th birthday appendix (§3.3) -- appended after the space's own block
# whenever the booking looks like an 18th (see _looks_like_18th, the same
# detection already used to flag this at enquiry time).
_EIGHTEENTH_APPENDIX = (
    "18th Birthday Conditions\n\n"
    "There are a few standard conditions that apply to all 18th birthday events:\n\n"
    "- A responsible adult, parent or guardian must be present for the full duration of the event and "
    "actively supervising.\n"
    "- All guests must present valid photo ID. ID and bag checks may be conducted at any time.\n"
    "- No BYO alcohol is permitted.\n"
    "- Strict RSA compliance applies. Underage guests cannot consume alcohol under any circumstances.\n"
    "- Management reserves the right to refuse entry or remove guests if needed.\n"
    "- The booking holder is legally responsible for all underage guests, guest behaviour, RSA compliance "
    "and any damages incurred.\n"
    "- If underage drinking or serious misconduct occurs, the event may be terminated immediately and you "
    f"will be charged a ${policy.CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD:.0f} per head cancellation fee to the "
    "numbers stated on the booking agreement. This is separate from and in addition to the general "
    "cancellation policy above."
)


def _guest_numbers_clause(space_name: str, agreed_min_adults: int) -> str:
    """Booking-specific, recomputed every time from Booking.agreed_min_adults
    (never Space.standard_min_adults) -- Master Policy v1.3 §4.4: never
    quote the standard minimum once a reduction is agreed, and never let
    the worked example go stale against the actual figure."""
    example = ""
    if agreed_min_adults > 10:
        shortfall_fee = policy.SHORTFALL_RATE_PER_ADULT * 10
        example = (
            f" For example, if only {agreed_min_adults - 10} guests attend, an additional "
            f"${shortfall_fee:.0f} fee will be charged to meet the minimum requirement."
        )
    return (
        f"{space_name} has a minimum requirement of {agreed_min_adults} guests. Final numbers must be "
        f"locked in 2 weeks prior to the event. On the night, if the guest count falls below "
        f"{agreed_min_adults}, a charge of ${policy.SHORTFALL_RATE_PER_ADULT:.0f} per person will apply for "
        f"the difference.{example} Kids are welcome but do not count toward minimum guest numbers."
    )


def _terms_text(booking: Booking) -> str:
    space_name = booking.space.name
    if space_name == "The Loft":
        minimum_spend_clause = _LOFT_MINIMUM_SPEND_CLAUSE
    elif space_name == "The Mezzanine":
        minimum_spend_clause = _MEZZANINE_MINIMUM_SPEND_CLAUSE
    else:
        # The Lounge (and anything else) has no Master Policy clause block
        # yet -- Aaron's own words: "flag and stop with a [REVIEW]... do
        # not invent terms." No guest minimum and no shortfall charge at
        # all for the Lounge, so a Guest Numbers clause here would be
        # actively wrong, not just unconfirmed.
        return f"{REVIEW} no Master Policy contract clause exists yet for {space_name} -- confirm with Aaron before sending"

    blocks = [
        ("Deposits", _DEPOSITS_CLAUSE),
        ("Credit Card", _CREDIT_CARD_CLAUSE),
        ("Guest Numbers", _guest_numbers_clause(space_name, booking.agreed_min_adults)),
        ("Minimum Spend", minimum_spend_clause),
        ("Booking Agreement", _BOOKING_AGREEMENT_CLAUSE),
        ("Decorations", _DECORATIONS_CLAUSE),
        ("Credit Card Surcharges", _CREDIT_CARD_SURCHARGE_CLAUSE),
        ("Cancellation Policy", _CANCELLATION_POLICY_CLAUSE),
        ("Public Holidays", _PUBLIC_HOLIDAYS_CLAUSE),
        ("Trading Hours", _TRADING_HOURS_CLAUSE),
    ]
    text = "\n\n".join(f"{heading}\n\n{body}" for heading, body in blocks)

    if looks_like_18th(booking.event_type or "", booking.event_name, booking.notes):
        text += "\n\n" + _EIGHTEENTH_APPENDIX

    return text


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


def _event_timeline_notes(booking: Booking) -> str:
    """setup_access_time/food_service_time are durable Booking columns
    (the wizard's Basics step writes straight to them, not to wizard-only
    JSONB state -- see app.models.wizard_session), so they're always
    available here regardless of which caller is generating the BEO, not
    just the wizard-sourced path. Only flags REVIEW when neither is
    actually on record yet."""
    parts = []
    if booking.setup_access_time is not None:
        confirmed = "confirmed" if booking.setup_access_confirmed else "requested, pending confirmation"
        parts.append(f"Setup access from {booking.setup_access_time.strftime('%H:%M')} ({confirmed}).")
    if booking.food_service_time is not None:
        parts.append(f"Food service from {booking.food_service_time.strftime('%H:%M')}.")
    if not parts:
        return f"{REVIEW} add run-sheet detail beyond start/end time"
    return " ".join(parts)


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
            "notes": _event_timeline_notes(booking),
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
    """Everything here is frozen into the document's content at generation
    time, including venue/ABN/address/contact -- deliberately the opposite
    of invoice.html's live venue globals (see app.templating). A signed
    contract has to reflect what was true when it was agreed, not whatever
    is true today; an unpaid invoice should always point at the current
    bank details."""
    space = booking.space
    return {
        "venue": policy.VENUE_TRADING_NAME,
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
        "terms_text": _terms_text(booking),
        "venue_abn": policy.VENUE_ABN,
        "venue_address": policy.VENUE_ADDRESS,
        "venue_contact_name": policy.VENUE_CONTACT_NAME,
        "venue_contact_email": policy.VENUE_CONTACT_EMAIL,
    }
