"""The triage gate: which enquiries may get an auto-drafted reply.

Phase 2 brief section 3, and the most important part of the phase. An
auto-draft is only appropriate where the answer is mechanical. Where
judgement is required the AI produces an internal note, not a
client-facing draft.

Two design rules, both deliberate:

FAIL CLOSED. Any condition this cannot evaluate -- a missing date, a
missing guest count, an unexpected error -- blocks the draft. The cost of
wrongly blocking is that a human writes the reply, which is what happens
today. The cost of wrongly drafting is a client receiving something that
should have been a conversation.

BUILT ON enquiry_classification, not beside it. looks_like_18th and the
accessibility pattern already exist and are already used by the enquiry
flagging and the agreement's 18th appendix. A second copy of "does this
look like an 18th" that could disagree with the first would be worse than
no gate at all.

The gate never writes anything. It reports a decision and the facts behind
it, so a blocked enquiry can carry a useful internal note ("40 guests
against the Loft's 60 minimum on a contested Saturday, six other parties
on that date") rather than a bare refusal.
"""

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Space
from app.services import availability, policy
from app.services.ai_availability import TOUCHING_STATUSES
from app.services.booking import times_overlap
from app.services.enquiry_classification import GENERIC_EVENT_TYPES, looks_like_18th
from app.services.validation import DAYTIME_CUTOFF, SATURDAY

logger = logging.getLogger(__name__)

# Reasons a draft is withheld. Stable codes so the shadow-mode comparison
# can count them (brief section 8: the gate should block roughly a third;
# a gate that never blocks is miscalibrated).
UNDER_18 = "under_18"
ACCESSIBILITY = "accessibility"
DIETARY = "dietary"
BELOW_MINIMUM = "below_minimum"
MULTI_SPACE = "multi_space"
OVER_CAPACITY = "over_capacity"
DAYTIME = "daytime"
DATE_TAKEN = "date_taken"
ROOM_HELD = "room_held"
NEGOTIATION = "negotiation"
BEREAVEMENT = "bereavement"
UNCLEAR = "unclear"
GATE_ERROR = "gate_error"

# Under-18 mention that is NOT an 18th birthday -- "a few guests under 18",
# "16 year olds", "my daughter is 17". looks_like_18th covers the event
# itself; this covers the guest list.
_UNDER_18_PATTERN = re.compile(
    r"under\s*-?\s*18|under\s*-?\s*eighteen|\bminors?\b|\bteenager|\b1[0-7]\s*(?:yo|y/o|year)|"
    r"\bschool\s*(?:formal|leavers)|\bkids?\b|\bchildren\b"
)

# Kitchen is 100% gluten free but NOT nut free, so this is a safety matter
# rather than a service one. Deliberately broad: over-blocking here costs a
# human ten minutes, under-blocking costs somebody an allergic reaction.
_DIETARY_PATTERN = re.compile(
    r"allerg|anaphyla|coeliac|celiac|gluten|nut\s*free|peanut|dairy\s*free|lactose|"
    r"vegan|vegetarian|halal|kosher|dietary|intoleran|epipen"
)

_MULTI_SPACE_PATTERN = re.compile(
    r"both\s+(?:rooms|spaces|areas)|whole\s+venue|entire\s+venue|exclusive\s+use|"
    r"two\s+(?:rooms|spaces)|all\s+(?:the\s+)?(?:rooms|spaces)|venue\s+hire|upstairs\s+and\s+downstairs"
)

_NEGOTIATION_PATTERN = re.compile(
    # \bdeal\b, not deal\b: unanchored on the left it matched inside
    # "ideal" -- "the Loft would be ideal for our team dinner" was being
    # blocked as a negotiation with a staff note that no budget had been
    # mentioned. Found by the 2026-09-05 review; it was inflating the
    # block rate with false positives, which is the calibration signal
    # shadow mode exists to measure.
    r"budget|discount|cheaper|negotiat|best\s+price|\bdeal\b|sponsor|donat|fundrais|"
    r"charit|not[\s-]for[\s-]profit|non[\s-]profit|nfp\b|afford"
)

_BEREAVEMENT_PATTERN = re.compile(r"\bwake\b|funeral|memorial|celebration\s+of\s+life|bereave|passed\s+away")

# Explicit daytime language, for enquiries with no times attached.
_DAYTIME_PATTERN = re.compile(
    # "morning tea" / "mid-morning" are daytime-event signals; a bare
    # "morning" is not -- it matched the greeting "Good morning!" on an
    # evening enquiry and blocked it as a daytime event with a factually
    # wrong staff note (2026-09-05 review).
    r"\blunch|\bdaytime\b|\bafternoon\b|\bbrunch\b|\bmorning\s+tea\b|\bmid-?morning\b|"
    r"high\s+tea|baby\s+shower|christening"
)


@dataclass
class GateBlock:
    code: str
    reason: str


@dataclass
class GateDecision:
    """should_draft is the only thing the caller acts on. blocks and facts
    exist so a blocked enquiry still produces something useful for staff."""

    should_draft: bool
    blocks: list[GateBlock] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [b.code for b in self.blocks]

    def as_note(self) -> str:
        """The internal note for a blocked enquiry -- what staff read in
        ten seconds instead of a draft."""
        if self.should_draft:
            return ""
        return " ".join(b.reason for b in self.blocks)


def _held_for_restaurant(space: Space, booking: Booking) -> bool:
    """Is this room held back for restaurant covers at this time?

    The Lounge on a Saturday night earns more as restaurant covers than as
    a function. Nothing else in the system knows that: the calendar shows
    the room genuinely empty and an availability check reports it free, so
    without this the gate would cheerfully offer a room Aaron would not.

    Times are usually absent on a form enquiry, so an unknown time on a
    Saturday is treated as the evening. A booking that explicitly finishes
    by the daytime cutoff is not the held slot.
    """
    if booking.event_date is None:
        return False
    if booking.event_date.weekday() != SATURDAY:
        return False
    if booking.end_time is not None and booking.end_time <= DAYTIME_CUTOFF:
        return False
    return space.name in policy.RESTAURANT_HELD_SATURDAY_EVENING


_MILESTONE = re.compile(r"\b(\d{1,3})\s*(?:st|nd|rd|th)\b")


def _milestone_age(event_name: str) -> int | None:
    """The birthday milestone if the name gives one ("Kyle's 30th" -> 30).

    None means the name does not say, which is the case the gate treats as
    unanswered rather than assuming adults.
    """
    match = _MILESTONE.search(event_name.lower())
    if match is None:
        return None
    try:
        age = int(match.group(1))
    except ValueError:
        return None
    return age if 1 <= age <= 120 else None


def _haystack(booking: Booking, extra: str = "") -> str:
    return " ".join(
        part
        for part in [booking.event_name, booking.event_type, booking.enquiry_text, booking.notes, extra]
        if part
    ).lower()


def _contention(db: Session, booking: Booking) -> list[Booking]:
    """Other live bookings and enquiries overlapping this room and time.

    Time-aware, matching the exclusion constraint: a lunch and an evening
    in one room are not competing for the same slot.
    """
    if booking.event_date is None:
        return []
    others = db.scalars(
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == booking.space.venue_id,
            Booking.event_date == booking.event_date,
            Booking.space_id == booking.space_id,
            Booking.id != booking.id,
            Booking.status.in_(TOUCHING_STATUSES),
        )
        .execution_options(populate_existing=True)
    ).all()
    return [o for o in others if times_overlap(booking, o)]


def _evaluate_candidate_rooms(db: Session, booking: Booking, guests: int, facts: dict) -> list[GateBlock]:
    """Which real rooms could take this enquiry, and is any of them free.

    Used only when no room has been chosen yet. A form enquiry arrives with
    no times either, just a date, so freeness is assessed for the whole day
    when times are absent. That is deliberately conservative -- a lunch
    booking will make the evening read as taken -- because over-blocking
    costs a human ten minutes and under-blocking offers a date that is
    gone.
    """
    rooms = list(
        db.scalars(
            select(Space)
            .where(Space.venue_id == booking.space.venue_id, Space.is_bookable.is_(True))
            .order_by(Space.capacity)
        ).all()
    )
    fitting = [r for r in rooms if guests <= r.capacity]
    held_back = [r for r in fitting if _held_for_restaurant(r, booking)]
    if held_back:
        facts["rooms_held_for_restaurant"] = [r.name for r in held_back]
        fitting = [r for r in fitting if r not in held_back]
    facts["rooms_that_fit"] = [r.name for r in fitting]

    if not fitting:
        largest = max((r.capacity for r in rooms), default=0)
        return [
            GateBlock(
                OVER_CAPACITY,
                f"{guests} guests is beyond every room (largest holds {largest}), so this needs declining well.",
            )
        ]

    free = []
    for room in fitting:
        if booking.event_date is None:
            continue
        if booking.start_time is not None and booking.end_time is not None:
            clash = db.scalars(
                select(Booking)
                .where(
                    Booking.space_id == room.id,
                    Booking.event_date == booking.event_date,
                    Booking.id != booking.id,
                    Booking.status.in_(TOUCHING_STATUSES),
                )
                .execution_options(populate_existing=True)
            ).all()
            if not any(times_overlap(booking, o) for o in clash):
                free.append(room)
        else:
            is_free, _ = availability.is_space_free(db, room.id, booking.event_date)
            if is_free:
                free.append(room)

    facts["rooms_free"] = [r.name for r in free]

    if not free:
        return [
            GateBlock(
                DATE_TAKEN,
                f"Every room that fits {guests} ({', '.join(r.name for r in fitting)}) is already booked "
                "that day. Which alternative to lead with, and whether to incentivise, is a sales call.",
            )
        ]

    if all(guests < r.standard_min_adults for r in free):
        smallest_minimum = min(r.standard_min_adults for r in free)
        return [
            GateBlock(
                BELOW_MINIMUM,
                f"{guests} guests is under the minimum of every free room (lowest is {smallest_minimum}). "
                "Whether to flex is commercial, and a reduced figure has to go into the agreement.",
            )
        ]

    return []


def evaluate(
    db: Session,
    booking: Booking,
    *,
    adult_count: int | None = None,
    attendee_count: int | None = None,
    enquiry_text: str = "",
) -> GateDecision:
    """Decide whether this enquiry may be auto-drafted.

    enquiry_text is the client's own free text. It is treated purely as
    something to pattern-match for risk signals -- never as instructions
    (brief section 7). Nothing in it can cause a draft to be produced; it
    can only cause one to be withheld.
    """
    try:
        return _evaluate(
            db, booking, adult_count=adult_count, attendee_count=attendee_count, enquiry_text=enquiry_text
        )
    except Exception:  # noqa: BLE001 -- fail closed, always
        logger.exception("Draft gate failed for booking %s", getattr(booking, "reference_code", "?"))
        return GateDecision(
            should_draft=False,
            blocks=[GateBlock(GATE_ERROR, "The triage gate could not be evaluated, so no draft was written.")],
        )


def _evaluate(
    db: Session,
    booking: Booking,
    *,
    adult_count: int | None,
    attendee_count: int | None,
    enquiry_text: str,
) -> GateDecision:
    blocks: list[GateBlock] = []
    facts: dict = {}
    text = _haystack(booking, enquiry_text)
    guests = attendee_count if attendee_count is not None else adult_count
    space = booking.space

    # --- unclear: never guess an ambiguous request ----------------------
    if booking.event_date is None:
        blocks.append(GateBlock(UNCLEAR, "No event date given, so nothing can be checked against a date."))
    if guests is None or guests <= 0:
        blocks.append(GateBlock(UNCLEAR, "No guest count given, which decides both the room and the minimum."))
    if (booking.event_type or "").strip().lower() in GENERIC_EVENT_TYPES:
        blocks.append(
            GateBlock(UNCLEAR, f"Event type is {booking.event_type or 'blank'!r}, so what they actually want is unclear.")
        )

    # --- under 18 -------------------------------------------------------
    # RSA conditions are communicated softly and in person. The AI has
    # previously invented a wristband procedure that would have handed
    # underage guests a workaround, so this never gets a draft.
    # A structured child count is as strong a signal as the words "kids"
    # or "18th" in the text, and until 2026-09-05 it was ignored entirely:
    # the form asks for adults and total attendees, the difference is
    # stored as child_count, and a 50-guest party with 20 children got an
    # auto-draft with no RSA conversation because nobody typed the word.
    # Aaron's call: any child on the booking blocks. If the rate goes up,
    # that is the honest rate.
    children = booking.child_count or 0
    if children > 0:
        noun = "child" if children == 1 else "children"
        blocks.append(
            GateBlock(
                UNDER_18,
                f"{children} {noun} on the booking, so guests are under 18 and the RSA conversation "
                "has to happen in person.",
            )
        )
    elif looks_like_18th(booking.event_type or "", booking.event_name or "", booking.notes) or _UNDER_18_PATTERN.search(text):
        blocks.append(
            GateBlock(UNDER_18, "An 18th, or guests under 18, so the RSA conversation has to happen in person.")
        )

    # --- accessibility --------------------------------------------------
    if re.search(r"\blift\b|wheelchair|accessib|stairs|mobility", text):
        accessible = db.scalars(
            select(Space).where(
                Space.venue_id == space.venue_id, Space.wheelchair_accessible.is_(True)
            )
        ).first()
        cap = accessible.capacity if accessible else 0
        facts["accessible_space"] = accessible.name if accessible else None
        facts["accessible_capacity"] = cap
        detail = f"Only {accessible.name} is step-free, capped at {cap}." if accessible else "No step-free space."
        blocks.append(GateBlock(ACCESSIBILITY, f"Accessibility raised. {detail} Needs raising honestly and early."))

    # --- dietary / allergy ----------------------------------------------
    if _DIETARY_PATTERN.search(text):
        blocks.append(
            GateBlock(DIETARY, "A dietary or allergy requirement. The kitchen is gluten free but not nut free, so this is a safety answer.")
        )

    # --- multi-space ----------------------------------------------------
    if _MULTI_SPACE_PATTERN.search(text) or booking.linked_bookings:
        blocks.append(GateBlock(MULTI_SPACE, "Two spaces or the whole venue: different minimums, two deposits, a different agreement."))

    # --- bereavement ----------------------------------------------------
    if _BEREAVEMENT_PATTERN.search(text):
        blocks.append(GateBlock(BEREAVEMENT, "A wake or funeral. Sensitive, and the package is still being defined."))

    # --- negotiation / discretionary pricing ----------------------------
    if _NEGOTIATION_PATTERN.search(text):
        blocks.append(GateBlock(NEGOTIATION, "Budget, a discount or a not-for-profit is mentioned, which is a discretionary call."))

    # --- a birthday whose milestone is unknown ---------------------------
    # enquiry_classification already flags exactly this at intake: a
    # generic "Birthday" with no milestone means the guest ages are
    # unknown, so the under-18 question is open and cannot be answered
    # from the form. Honouring that flag is the point of building on
    # enquiry_classification rather than beside it.
    #
    # Scoped to birthdays where no milestone is discernible. "Kyle's 30th"
    # answers the question; a bare "Birthday" does not. Blocking every
    # birthday would block most enquiries and make the gate useless.
    if (booking.event_type or "").strip().lower() == "birthday" and not any(
        b.code == UNDER_18 for b in blocks
    ):
        milestone = _milestone_age(booking.event_name or "")
        if milestone is None:
            blocks.append(
                GateBlock(
                    UNDER_18,
                    "A birthday with no milestone given, so whether any guests are under 18 is "
                    "unanswered. Ask before drafting.",
                )
            )
        elif milestone <= 18:
            # 18 itself is the case this rule exists for: "Eva's 18th" with
            # no "birthday" in the text is still an 18th.
            blocks.append(
                GateBlock(UNDER_18, f"A {milestone}th birthday, so guests are under 18 or turning 18.")
            )

    # --- capacity and minimums ------------------------------------------
    if guests is not None and guests > 0:
        if space.is_bookable:
            largest = db.scalar(
                select(func.max(Space.capacity)).where(
                    Space.venue_id == space.venue_id, Space.is_bookable.is_(True)
                )
            )
            facts["largest_capacity"] = largest
            if largest is not None and guests > largest:
                blocks.append(
                    GateBlock(OVER_CAPACITY, f"{guests} guests is beyond every space (largest holds {largest}), so this needs declining well.")
                )
            elif guests < booking.agreed_min_adults:
                # The AGREED minimum, never the space default: a booking
                # Aaron has already flexed must not be blocked as "below
                # minimum" against a figure that no longer applies to it
                # (agreed_min_adults defaults to the space standard on
                # creation, so an unflexed booking compares the same way).
                facts["space_minimum"] = space.standard_min_adults
                facts["agreed_minimum"] = booking.agreed_min_adults
                flexed = booking.agreed_min_adults != space.standard_min_adults
                blocks.append(
                    GateBlock(
                        BELOW_MINIMUM,
                        f"{guests} guests against {space.name}'s "
                        f"{'agreed' if flexed else 'standard'} minimum of {booking.agreed_min_adults}. "
                        "Whether to flex is commercial, and a reduced figure has to go into the agreement.",
                    )
                )
        else:
            # No room chosen yet -- every form enquiry starts here, on the
            # "Unassigned (pending triage)" placeholder. Its capacity and
            # minimum are both 0, so checking against IT would silently pass
            # everything, and checking contention against it would compare
            # one unassigned enquiry with another instead of with the real
            # rooms. Evaluate against the rooms that could actually take it.
            blocks.extend(_evaluate_candidate_rooms(db, booking, guests, facts))

    # --- daytime --------------------------------------------------------
    # Turnaround rules and the "stay later if nothing's booked" caveat are
    # a judgement call, so any daytime shape is a human's.
    if booking.end_time is not None and booking.end_time <= DAYTIME_CUTOFF:
        blocks.append(GateBlock(DAYTIME, "A daytime event, where turnaround and the stay-later caveat are a judgement call."))
    elif booking.start_time is None and _DAYTIME_PATTERN.search(text):
        blocks.append(GateBlock(DAYTIME, "Reads as a daytime event, where turnaround and the stay-later caveat are a judgement call."))

    # --- a room held back for the restaurant ------------------------------
    if space.is_bookable and _held_for_restaurant(space, booking):
        blocks.append(
            GateBlock(
                ROOM_HELD,
                f"{space.name} is held for restaurant covers on a Saturday night. Offering it anyway "
                "is a yield decision, not an automatic one.",
            )
        )

    # --- the date itself ------------------------------------------------
    rivals = _contention(db, booking)
    facts["contested_by"] = [o.reference_code for o in rivals]
    taken = [o for o in rivals if o.status.value in ("tentative", "confirmed", "completed")]
    if taken:
        facts["taken_by"] = [o.reference_code for o in taken]
        blocks.append(
            GateBlock(
                DATE_TAKEN,
                f"{space.name} is already held that night ({len(taken)} booking(s)). "
                "Which alternative to lead with, and whether to incentivise, is a sales call.",
            )
        )
    elif rivals:
        # Not a block. A contested but free slot may still be drafted, and
        # the draft must disclose the other interest (brief section 4).
        facts["open_enquiry_count"] = len(rivals)

    return GateDecision(should_draft=not blocks, blocks=blocks, facts=facts)
