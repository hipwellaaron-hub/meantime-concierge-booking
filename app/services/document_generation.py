"""Auto-populates document content from structured booking data, and
nothing else -- anywhere the data doesn't exist yet (no Handover doc was
available at build time; payments don't exist until Phase 3), this emits
an explicit [REVIEW] marker rather than guessing. Both silent guessing and
hard blocking are wrong; this is the middle path.
"""

import datetime as dt
from decimal import Decimal, InvalidOperation

from app.models import Booking
from app.services import policy
from app.services.enquiry_classification import looks_like_18th
from app.utils import format_person_name
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

# 18th birthday appendix (§3.3) -- appended as its own final clause
# whenever the booking looks like an 18th (see looks_like_18th, the same
# detection already used to flag this at enquiry time). No heading line in
# the body itself: "18th Birthday Conditions" is the section's own heading.
_EIGHTEENTH_APPENDIX = (
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


def rebuild_terms_text(terms_sections: list[dict]) -> str:
    """The flattened plain-text form of terms_sections, kept alongside the
    structured form for anything that wants one block of text (and so the
    existing content["terms_text"] substring assertions in
    tests/test_documents.py need no change). Shared by generation and by
    the document edit save path, so the two never drift out of the same
    "Heading\\n\\nBody" join format."""
    return "\n\n".join(f"{section['heading']}\n\n{section['body']}" for section in terms_sections)


def _terms_sections(booking: Booking) -> list[dict]:
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
        # actively wrong, not just unconfirmed. Still a one-item section
        # list (not a bare string) so the template and the editor can
        # treat every space the same way -- and so Aaron can type the
        # Lounge's real terms in directly via the editor once they exist.
        return [{
            "heading": "Terms",
            "body": f"{REVIEW} no Master Policy contract clause exists yet for {space_name} -- confirm with Aaron before sending",
        }]

    sections = [
        {"heading": "Deposits", "body": _DEPOSITS_CLAUSE},
        {"heading": "Credit Card", "body": _CREDIT_CARD_CLAUSE},
        {"heading": "Guest Numbers", "body": _guest_numbers_clause(space_name, booking.agreed_min_adults)},
        {"heading": "Minimum Spend", "body": minimum_spend_clause},
        {"heading": "Booking Agreement", "body": _BOOKING_AGREEMENT_CLAUSE},
        {"heading": "Decorations", "body": _DECORATIONS_CLAUSE},
        {"heading": "Credit Card Surcharges", "body": _CREDIT_CARD_SURCHARGE_CLAUSE},
        {"heading": "Cancellation Policy", "body": _CANCELLATION_POLICY_CLAUSE},
        {"heading": "Public Holidays", "body": _PUBLIC_HOLIDAYS_CLAUSE},
        {"heading": "Trading Hours", "body": _TRADING_HOURS_CLAUSE},
    ]

    if looks_like_18th(booking.event_type or "", booking.event_name, booking.notes):
        sections.append({"heading": "18th Birthday Conditions", "body": _EIGHTEENTH_APPENDIX})

    return sections


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


def format_time_12h(value: dt.time | None) -> str:
    """"6:30pm" -- the reference Event Order's own time style. Composed by
    hand rather than strftime %-I / %#I, which differ between platforms."""
    if value is None:
        return f"{REVIEW} time not yet finalized"
    hour = value.hour % 12 or 12
    suffix = "pm" if value.hour >= 12 else "am"
    return f"{hour}:{value.minute:02d}{suffix}"


def format_date_long(value: dt.date | None) -> str:
    """"Saturday, 29 August 2026" -- day number composed by hand for the
    same platform-portability reason as format_time_12h."""
    if value is None:
        return f"{REVIEW} event date not yet confirmed"
    return f"{value.strftime('%A')}, {value.day} {value.strftime('%B %Y')}"


def format_day_date(value: dt.date) -> str:
    """"Wednesday 26 August" -- the AV USB deadline style. Always an
    absolute date; a relative phrase ("in 2 days") goes stale the moment
    the document outlives the day it was generated."""
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B')}"


def build_event_timeline(booking: Booking, vendors: list[dict] | None = None) -> dict:
    """The Event Timeline block: run-order bullets the floor team acts on.
    Shared by generation AND the BEO edit screen's save path (same
    reasoning as build_total_food_spend) so the two can never drift.

    vendors, when given, is build_vendor_snapshot output -- a vendor with
    a bump-in time belongs here beside setup access and guest arrival,
    because it's a time someone must act on; vendors without one render in
    Special Notes instead (see build_special_notes).

    Two bullets are appended automatically from operational policy, never
    hand-entered: "Music off by 11:30pm" on every evening booking, and the
    Saturday-daytime 5:00pm hard stop on any Saturday booking starting
    before the cutoff -- both sourced from app.services.validation's
    constants, so the document can't drift from what booking-time
    validation enforces.
    """
    from app.services.validation import DAYTIME_CUTOFF, MUSIC_OFF_TIME, SATURDAY

    bullets: list[str] = []
    if booking.setup_access_time is not None:
        confirmed = "confirmed" if booking.setup_access_confirmed else "requested, pending confirmation"
        bullets.append(f"Setup access from {format_time_12h(booking.setup_access_time)} ({confirmed})")
    for vendor in vendors or []:
        if vendor.get("bump_in_display"):
            bullets.append(vendor["bump_in_display"])
    if booking.guest_arrival_time is not None:
        bullets.append(f"Guests arrive {format_time_12h(booking.guest_arrival_time)}")
    if booking.food_service_time is not None:
        bullets.append(f"Food service from {format_time_12h(booking.food_service_time)}")
    for moment in booking.key_moments or []:
        label = (moment.get("label") or "").strip()
        if not label:
            continue
        time_str = moment.get("time")
        if time_str:
            parsed = dt.time.fromisoformat(time_str)
            bullets.append(f"{format_time_12h(parsed)} — {label}")
        else:
            bullets.append(label)

    if booking.event_date is not None and booking.start_time is not None:
        is_saturday_daytime = booking.event_date.weekday() == SATURDAY and booking.start_time < DAYTIME_CUTOFF
        if is_saturday_daytime:
            bullets.append(f"Event concludes by {format_time_12h(DAYTIME_CUTOFF)} (Saturday daytime)")
    # "Evening" = runs past the daytime cutoff -- same boundary the
    # Saturday rule uses, so the two bullets can't disagree about what
    # counts as evening.
    if booking.end_time is not None and booking.end_time > DAYTIME_CUTOFF:
        bullets.append(f"Music off by {format_time_12h(MUSIC_OFF_TIME)}")

    if booking.pack_down_notes:
        bullets.append(f"Pack-down/collection: {booking.pack_down_notes}")

    return {
        "event_date": _format_date(booking.event_date),
        "event_date_display": format_date_long(booking.event_date),
        "start_time": _format_time(booking.start_time),
        "end_time": _format_time(booking.end_time),
        "start_time_display": format_time_12h(booking.start_time),
        "end_time_display": format_time_12h(booking.end_time),
        "bullets": bullets,
        # Legacy field kept so old template fallbacks and non-overhauled
        # callers keep rendering something sensible.
        "notes": _event_timeline_notes(booking),
    }


def build_av_block(booking: Booking, av_response: dict | None) -> dict | None:
    """The Loft-only AV/Screen section. None for every other space -- the
    screen is physically in The Loft, so the section must not exist at all
    elsewhere, not render empty."""
    if booking.space.name != "The Loft" or not av_response:
        return None
    deadline_display = None
    if booking.event_date is not None:
        deadline = booking.event_date - dt.timedelta(days=policy.AV_USB_DEADLINE_DAYS_BEFORE_EVENT)
        deadline_display = format_day_date(deadline)
    return {
        "video_slideshow": bool(av_response.get("video_slideshow")),
        "format_note": "USB flash drive only — phones and laptops cannot connect to the venue screen",
        "usb_deadline_display": deadline_display,
        "microphones_for_speeches": bool(av_response.get("microphones_for_speeches")),
        "notes": av_response.get("notes"),
    }


# Display labels for vendor types -- .title() alone would render "Dj".
VENDOR_TYPE_LABELS = {
    "dj": "DJ",
    "band": "Band",
    "decorator": "Decorator",
    "photographer": "Photographer",
    "other": "Vendor",
}


def vendor_type_label(vendor_type: str) -> str:
    return VENDOR_TYPE_LABELS.get(vendor_type, vendor_type.title())


def build_vendor_snapshot(vendors) -> list[dict]:
    """Display-ready vendor lines frozen into the BEO's content. The
    requested-vs-confirmed wording is composed HERE, once, so the template
    can never drift into presenting a client's requested bump-in as an
    agreed time -- a client routinely nominates a time their DJ never
    agreed to (see BookingVendor.bump_in_confirmed)."""
    snapshot = []
    for vendor in vendors:
        display = None
        if vendor.bump_in_time is not None:
            state = "confirmed" if vendor.bump_in_confirmed else "requested — not yet confirmed"
            contact = f", {vendor.contact_number}" if vendor.contact_number else ""
            display = (
                f"{vendor_type_label(vendor.vendor_type)} bump-in {format_time_12h(vendor.bump_in_time)} "
                f"({state}) — {vendor.name}{contact}"
            )
        snapshot.append(
            {
                "vendor_type": vendor.vendor_type,
                "name": vendor.name,
                "contact_number": vendor.contact_number,
                "bump_in_time": vendor.bump_in_time.strftime("%H:%M") if vendor.bump_in_time else None,
                "bump_in_confirmed": vendor.bump_in_confirmed,
                "bump_in_display": display,
            }
        )
    return snapshot


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


def build_total_food_spend(food_total: Decimal | None, deposit_paid: Decimal | None) -> dict:
    """The total_food_spend block, derived from the two figures it depends
    on. Shared by generation and by the document edit save path so an
    edited food order can never keep a note that contradicts its own
    numbers (e.g. still saying "add a food order" once one exists)."""
    if food_total is None:
        note = f"{REVIEW} add a food order above to compute the total"
        balance_due = None
    elif deposit_paid is None:
        note = (
            f"{REVIEW} deposit paid / balance due aren't derivable yet -- "
            "payments aren't tracked in Concierge until Phase 3"
        )
        balance_due = None
    else:
        note = None
        balance_due = food_total - deposit_paid

    return {
        "total": str(food_total) if food_total is not None else None,
        "deposit_paid": str(deposit_paid) if deposit_paid is not None else None,
        "balance_due": str(balance_due) if balance_due is not None else None,
        "note": note,
    }


def generate_beo_content(
    booking: Booking,
    food_order_line_items: list[dict] | None = None,
    *,
    catering_order_and_service_style: str | None = None,
    bar_structure: str | None = None,
    room_layout_notes: str | None = None,
    music: str | None = None,
    entertainment: str | None = None,
    music_entertainment: str | None = None,  # legacy merged field, kept for non-wizard callers
    dietaries: str | None = None,
    accessibility: str | None = None,
    decorations: str | None = None,
    status_text: str | None = None,
    special_notes_extra: str | None = None,
    av: dict | None = None,
    vendors: list[dict] | None = None,
    internal_notes: str | None = None,
    onsite_contact: str | None = None,
    deposit_paid: Decimal | None = None,
    total_paid: Decimal | None = None,
) -> dict:
    """Every keyword-only param defaults to None, which preserves the
    original [REVIEW]-placeholder behavior for existing callers -- this
    function still has no opinion on what's genuinely missing vs. real
    captured data with nothing outstanding; that distinction is the
    caller's job (see app.services.wizard_generation).

    The [REVIEW] markers baked into fields here are STAFF-facing prompts.
    They must never reach a client: the template renders every free-text
    field through the client_safe filter (app.templating), which swaps
    them for a neutral placeholder on the public view and the PDF.

    Dietaries is the one field that can never be silently absent: whether
    or not a value was captured, the key always carries an explicit answer
    -- a declared allergy going quietly missing between capture and
    document is the exact failure this guards against.
    """
    food_order_line_items = food_order_line_items or []
    food_total = compute_food_order_total(food_order_line_items)
    contact = booking.contact

    return {
        "event_timeline": build_event_timeline(booking, vendors),
        "catering_order_and_service_style": catering_order_and_service_style or f"{REVIEW} add catering order and service style",
        "food_order": {
            "line_items": food_order_line_items,
            "note": None if food_order_line_items else f"{REVIEW} no food order captured yet",
        },
        "total_food_spend": build_total_food_spend(food_total, deposit_paid),
        "bar_structure": bar_structure or f"{REVIEW} add bar structure",
        "room_layout_notes": room_layout_notes if room_layout_notes is not None else f"{REVIEW} add room layout notes",
        "music": music,
        "entertainment": entertainment,
        # Legacy merged field: populated only when the split music field
        # isn't, preserving the pre-overhaul behavior (including its
        # staff-facing [REVIEW] prompt) byte for byte for old callers.
        "music_entertainment": (
            (music_entertainment or f"{REVIEW} add music/entertainment detail") if music is None else None
        ),
        "dietaries": dietaries or "No dietary requirements declared",
        "accessibility": accessibility,
        "decorations": decorations,
        "status_text": status_text,
        "special_notes": special_notes_extra if special_notes_extra is not None else (booking.notes or ""),
        "av": av,
        "vendors": vendors or [],
        # Staff/kitchen only -- the template structurally excludes this
        # from the client view and the PDF (see document.html).
        "internal_notes": internal_notes,
        "onsite_contact": onsite_contact,
        "status": booking.status.value,
        "_reference": {
            "reference_code": booking.reference_code,
            "event_name": booking.event_name,
            "space_name": booking.space.name,
            "adult_count": booking.adult_count,
            "child_count": booking.child_count,
            # Display-cased: a client who typed "ruby hipwell" into the
            # form should not headline her own Event Order in lowercase.
            # The stored Contact record keeps what was typed.
            "client_name": format_person_name(contact.name) if contact else None,
            "client_phone": contact.phone if contact else None,
            "total_paid": str(total_paid) if total_paid is not None else None,
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
    terms_sections = _terms_sections(booking)
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
        # Both forms are stored: terms_sections is what the template
        # renders (real per-clause headings) and what the staff editor
        # edits; terms_text is the same content flattened, for anything
        # that wants one block of plain text.
        "terms_sections": terms_sections,
        "terms_text": rebuild_terms_text(terms_sections),
        "venue_abn": policy.VENUE_ABN,
        "venue_address": policy.VENUE_ADDRESS,
        "venue_contact_name": policy.VENUE_CONTACT_NAME,
        "venue_contact_email": policy.VENUE_CONTACT_EMAIL,
    }
