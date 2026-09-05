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
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Space
from app.models.booking import BLOCKING_STATUSES
from app.services import policy
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
# Two or more unassigned enquiries chasing one date: no room reads as
# taken, but they are competing for the same pool.
CONTESTED = "contested"
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
    # The first tightening lost "mid morning" (space), "morning teas" and
    # "Sunday morning" (wave 2 review); the day-of-week and "in the" forms
    # bring back the real daytime uses of "morning" without the greeting.
    r"\blunch|\bdaytime\b|\bafternoon\b|\bbrunch\b|\bbreakfast\b|"
    r"\bmorning[\s-]+teas?\b|\bmid[\s-]?morning\b|"
    # Not "in the morning": "drop the decorations off in the morning" is
    # logistics on an evening enquiry (re-review).
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+morning\b|"
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
    _start, end, _from_form = _effective_times(booking)
    if end is not None and end <= DAYTIME_CUTOFF:
        return False
    return space.name in policy.RESTAURANT_HELD_SATURDAY_EVENING


_MILESTONE = re.compile(r"\b(\d{1,3})\s*(?:st|nd|rd|th)\b")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


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
        for part in [
            booking.event_name, booking.event_type, booking.enquiry_text, booking.notes,
            booking.proposed_time_slot, extra,
        ]
        if part
    ).lower()


# The form's "Proposed Time" is free text ("6pm to 11pm", "12:00 PM - 5:00
# PM"). Until 2026-09-05 the gate never read it, so an enquiry that named
# its hours was still treated as taking the whole day. Aaron's call: use
# it when it parses, fall back to the whole day when it is empty or
# unreadable. Only a clear two-ended range counts; "6pm till late",
# "evening", "lunch" and a bare "6 to 11" all fall back.
_TIME_TOKEN = re.compile(r"(?<![\d:])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d:])", re.I)
_RANGE_SEPARATOR = re.compile(r"\s*(?:-|\u2013|\u2014|to|till|til|until)\s*", re.I)
_MIDNIGHT = re.compile(r"\b(?:12\s*)?midnight\b", re.I)
_NOON = re.compile(r"\b(?:12\s*)?(?:noon|midday)\b", re.I)
# The only words allowed to sit around the range. Anything else -- a
# guest count, a date, "arrival", "TBC", "plus", "?", a second time --
# means the field says more than the range, and the gate does not guess.
_HARMLESS = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun "
    "morning afternoon evening night lunch dinner drinks from on at the a an".split()
)
_RESIDUE_PUNCTUATION = re.compile(r"[\s,.;:()\-\u2013\u2014]+")


def parse_time_range(text: str | None) -> tuple[dt.time, dt.time] | None:
    """A (start, end) pair from free text, or None when the field is not
    simply the event's two-ended clock range.

    The field is client free text, and every reading the gate trusts
    narrows contention, so the policy is: parse only what is unambiguous
    and fall back to the whole day for everything else. Four review
    passes each found a shape a looser rule let through ("12/11 - 6pm"
    as 11:00-18:00, "5-6pm arrival" as the whole night, "6pm-8pm dinner
    and dancing after", "5 for 6pm-11pm", "6pm to 11pm plus"), and an
    allow-list of bad words cannot be finished. So the rule is the
    reverse: the field must be the range and nothing else.
    - Both ends carry am/pm ("6pm to 11pm", "12:00 PM - 5:00 PM"), or
      both carry minutes on a 24-hour clock with an hour past 12
      ("18:00-23:00"). "6-11pm", "6pm to 12", "10-2pm" fall back: the
      client is not harmed by the whole day, and the gate is not
      guessing which side the am/pm belongs to.
    - The two ends are adjacent and joined by a range word or dash, and
      whatever else is in the field is a weekday or a word like
      "evening"; any other word, digit or symbol falls back.
    - "12am"/"midnight"/"12:00am" as the end means the end of that day;
      "12am" as a start (lay usage for noon) and anything past midnight
      fall back."""
    if not text:
        return None
    text = _NOON.sub("12pm", _MIDNIGHT.sub("12am", text))
    tokens = list(_TIME_TOKEN.finditer(text))

    def meridiem(match):
        raw = match.group(3)
        return raw.lower().replace(".", "")[:2] if raw else None

    def clockish(match):
        return bool(match.group(2) or match.group(3))

    pairs = [
        (a, b)
        for a, b in zip(tokens, tokens[1:])
        if clockish(a) and clockish(b) and _RANGE_SEPARATOR.fullmatch(text[a.end():b.start()])
    ]
    if len(pairs) != 1:
        return None
    first, second = pairs[0]
    residue = text[:first.start()] + " " + text[second.end():]
    if any(word.lower() not in _HARMLESS for word in _RESIDUE_PUNCTUATION.split(residue) if word):
        return None

    def build(match, ampm, *, is_end=False):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if minute > 59:
            return None
        if ampm:
            if not 1 <= hour <= 12:
                return None
            if hour == 12 and ampm == "am":
                return dt.time(23, 59) if is_end and minute == 0 else None
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        elif hour > 23:
            return None
        return dt.time(hour, minute)

    m1, m2 = meridiem(first), meridiem(second)
    h1, h2 = int(first.group(1)), int(second.group(1))
    if m1 and m2:
        pass
    elif m1 is None and m2 is None and first.group(2) and second.group(2) and (h1 > 12 or h2 > 12):
        pass
    else:
        return None
    start_t, end_t = build(first, m1), build(second, m2, is_end=True)
    if start_t is None or end_t is None or not start_t < end_t:
        return None
    return start_t, end_t


def _effective_times(booking: Booking) -> tuple[dt.time | None, dt.time | None, bool]:
    """The times the gate reasons with: the booking's own when either is
    set, otherwise the form's Proposed Time when it parses. The flag says
    the form supplied them. (None, None, False) means the whole day.

    Two deliberate limits (re-review). A staff-set start with no end is
    still the booking's own time and is not overridden by the client's
    earlier free text. And a hold or confirmation with no real times is
    never narrowed by its Proposed Time: until staff pin it, it holds the
    whole day -- the same rule _unassigned_holds applies on the other
    branch, so a hold is not whole-day for one enquiry and 12-to-5 for
    the next."""
    if booking.start_time is not None or booking.end_time is not None:
        return booking.start_time, booking.end_time, False
    if booking.status in BLOCKING_STATUSES:
        return None, None, False
    parsed = parse_time_range(booking.proposed_time_slot)
    if parsed is not None:
        return parsed[0], parsed[1], True
    return None, None, False


def _gate_overlaps(a: Booking, b: Booking) -> bool:
    """Overlap as the GATE sees it: a booking with no times contends for
    the whole day.

    Deliberately NOT booking.times_overlap. That function mirrors the
    database exclusion constraint -- a NULL range never conflicts -- and
    is shared by the supersede cascade and reconciliation, so widening it
    there would change what kills a rival. The gate asks a different
    question: is this date safe to offer? A form enquiry arrives with no
    times, and treating "no time" as "no clash" is how two enquiries for
    the same Saturday were each told the date was clear (2026-09-05
    review). Overstating contention costs a human ten minutes;
    understating it sells the same night twice. Aaron's call: contend on
    date and space, and a missing time means the whole day -- where "time"
    includes the form's Proposed Time when it parses (_effective_times).

    The overlap arithmetic itself is still times_overlap's, handed the
    effective times, so there is one definition of "overlap" in the
    system and only the "no time" rule differs."""
    a_start, a_end, _ = _effective_times(a)
    b_start, b_end, _ = _effective_times(b)
    if None in (a_start, a_end, b_start, b_end):
        return True
    return times_overlap(
        SimpleNamespace(start_time=a_start, end_time=a_end),
        SimpleNamespace(start_time=b_start, end_time=b_end),
    )


def _contention(db: Session, booking: Booking) -> list[Booking]:
    """Other live bookings and enquiries overlapping this room and time.

    Time-aware when both sides have times: a lunch and an evening in one
    room are not competing for the same slot. A side with no times
    contends for the whole day (see _gate_overlaps). For a booking still
    on the Unassigned placeholder, "this room" is the placeholder itself,
    so the rivals found here are the OTHER unassigned enquiries on the
    date -- the set the caller blocks on.
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
    if not booking.space.is_bookable:
        # The placeholder is not a room, so "same slot" has no meaning
        # there: two enquiries with no room yet are competing for the same
        # pool whatever hours they wrote, and which one gets which room is
        # the sales call (Aaron: contend on date and space, not time). The
        # Proposed Time narrows an enquiry against REAL rooms only.
        return others
    return [o for o in others if _gate_overlaps(booking, o)]


def _unassigned_holds(db: Session, booking: Booking) -> list[Booking]:
    """Confirmed or tentative bookings on this date that still have no
    room -- an iVvy import not yet triaged, or a hold walked to tentative
    from the placeholder. They hold no room, but they WILL take one.
    Aaron's call (2026-09-05): a confirmed booking with no room is a data
    problem to fix, not a case to reason around; until it is assigned it
    holds the whole day, so no draft goes out for that date."""
    if booking.event_date is None:
        return []
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == booking.space.venue_id,
                Space.is_bookable.is_(False),
                Booking.event_date == booking.event_date,
                Booking.id != booking.id,
                Booking.status.in_(BLOCKING_STATUSES),
            )
            .execution_options(populate_existing=True)
        ).all()
    )


def _evaluate_candidate_rooms(
    db: Session, booking: Booking, guests: int, adults: int, facts: dict
) -> list[GateBlock]:
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

    if booking.event_date is None:
        # UNCLEAR already says there is no date. Falling through used to
        # leave `free` empty and invent "every room is already booked that
        # day" for a date that does not exist (wave 2 review).
        return []

    free: list[Space] = []
    occupied: dict[str, list[str]] = {}
    for room in fitting:
        # One path for timed and untimed alike. The untimed case used to
        # fall back to availability.is_space_free, which counts only
        # BLOCKING statuses -- so an `offered` or open `enquiry` already
        # sitting on a room made that room read as free to a form enquiry
        # with no times. TOUCHING_STATUSES is a superset of
        # BLOCKING_STATUSES, so this is strictly more conservative than
        # what it replaces, and _gate_overlaps makes a missing time mean
        # the whole day.
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
        clashing = [o for o in clash if _gate_overlaps(booking, o)]
        if clashing:
            occupied[room.name] = [f"{o.reference_code} ({o.status.value})" for o in clashing]
        else:
            free.append(room)

    facts["rooms_free"] = [r.name for r in free]

    if not free:
        # Say what is actually on the rooms. An open enquiry occupying a
        # room is not "booked", and the note was claiming it was (wave 2
        # review); staff also need the references to look up.
        facts["rooms_occupied_by"] = occupied
        detail = "; ".join(f"{room}: {', '.join(refs)}" for room, refs in occupied.items())
        return [
            GateBlock(
                DATE_TAKEN,
                f"Every room that fits {guests} ({', '.join(r.name for r in fitting)}) already has a "
                f"booking, hold or open enquiry on it that day ({detail}). "
                "Which alternative to lead with, and whether to incentivise, is a sales call.",
            )
        ]

    # Minimums are ADULT minimums; capacity counts everyone. Comparing the
    # total headcount against the adult minimum let 35 adults and 10
    # children through a 40-adult minimum (wave 2 review). A free room
    # whose minimum the party does NOT meet is still free, but it is not
    # one the draft may offer: flexing a minimum is a commercial call
    # (re-review -- rooms_free alone was being stamped "offerable").
    facts["rooms_offerable"] = [r.name for r in free if adults >= r.standard_min_adults]
    if all(adults < r.standard_min_adults for r in free):
        smallest_minimum = min(r.standard_min_adults for r in free)
        return [
            GateBlock(
                BELOW_MINIMUM,
                f"{adults} adults is under the minimum of every free room (lowest is {smallest_minimum}). "
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
    adults = adult_count if adult_count is not None else guests
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
    eighteenth = looks_like_18th(booking.event_type or "", booking.event_name or "", booking.notes)
    # "Eva's 18th" with type "birthday" is caught by the milestone rule
    # further down, which only runs when nothing above has said UNDER_18.
    milestone = (
        _milestone_age(booking.event_name or "")
        if (booking.event_type or "").strip().lower() == "birthday"
        else None
    )
    if children > 0:
        noun = "child" if children == 1 else "children"
        reason = (
            f"{children} {noun} on the booking, so guests are under 18 and the RSA conversation "
            "has to happen in person."
        )
        # Both signals at once: the 18th is the fact staff act on (the
        # 18th appendix), so it must not be lost behind the headcount.
        if eighteenth or milestone == 18:
            reason += " It also reads as an 18th."
        elif milestone is not None and milestone < 18:
            reason += f" It also reads as a {_ordinal(milestone)} birthday."
        blocks.append(GateBlock(UNDER_18, reason))
    elif eighteenth or _UNDER_18_PATTERN.search(text):
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
                GateBlock(UNDER_18, f"A {_ordinal(milestone)} birthday, so guests are under 18 or turning 18.")
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
            elif adults < booking.agreed_min_adults:
                # The AGREED minimum, never the space default: a booking
                # Aaron has already flexed must not be blocked as "below
                # minimum" against a figure that no longer applies to it
                # (agreed_min_adults defaults to the space standard on
                # creation, so an unflexed booking compares the same way).
                # And ADULTS against an adult minimum: the total headcount
                # was letting 35 adults and 10 children through a 40-adult
                # minimum (wave 2 review).
                facts["space_minimum"] = space.standard_min_adults
                facts["agreed_minimum"] = booking.agreed_min_adults
                flexed = booking.agreed_min_adults != space.standard_min_adults
                blocks.append(
                    GateBlock(
                        BELOW_MINIMUM,
                        f"{adults} adults against {space.name}'s "
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
            blocks.extend(_evaluate_candidate_rooms(db, booking, guests, adults, facts))

    # --- daytime --------------------------------------------------------
    # Turnaround rules and the "stay later if nothing's booked" caveat are
    # a judgement call, so any daytime shape is a human's.
    start, end, from_form = _effective_times(booking)
    if from_form:
        facts["times_from_form"] = f"{start:%H:%M}-{end:%H:%M}"
    if end is not None and end <= DAYTIME_CUTOFF:
        source = " (from the form's proposed time)" if from_form else ""
        blocks.append(GateBlock(DAYTIME, f"A daytime event{source}, where turnaround and the stay-later caveat are a judgement call."))
    elif (start is None or from_form) and _DAYTIME_PATTERN.search(text):
        # The text rule stands down only for STAFF-set times. A form that
        # says "6pm to 11pm" in one field and "morning tea" in another
        # contradicts itself, which is an ask-before-drafting case, not a
        # settled evening (re-review).
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
    if space.is_bookable:
        taken = [o for o in rivals if o.status in BLOCKING_STATUSES]
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
            # A contested but free slot may still be drafted, and the
            # draft must disclose the other interest (brief section 4).
            facts["open_enquiry_count"] = len(rivals)
        holds = _unassigned_holds(db, booking)
        if holds:
            facts["unassigned_holds"] = [o.reference_code for o in holds]
            listed = ", ".join(f"{o.reference_code} ({o.status.value})" for o in holds)
            verb = "has" if len(holds) == 1 else "have"
            blocks.append(
                GateBlock(
                    CONTESTED,
                    f"{len(holds)} confirmed or tentative booking(s) on this date still {verb} no room "
                    f"assigned ({listed}). Assign {'it' if len(holds) == 1 else 'them'} before offering {space.name}.",
                )
            )
    elif rivals:
        # Two or more unassigned enquiries chasing the same date. No room
        # reads as taken, because none of them holds one -- but they are
        # competing for the same pool, and which one gets which room is a
        # sales call, not a draft. Aaron's call (2026-09-05): block, do not
        # disclose, and contention is contention whatever the rival's
        # status -- an offered or tentative one on the placeholder is
        # still competing for a room, and DATE_TAKEN "Unassigned is already
        # held" named the placeholder as if it were a room (wave 2 review).
        noun = "enquiry or hold" if len(rivals) == 1 else "enquiries or holds"
        listed = ", ".join(f"{o.reference_code} ({o.status.value})" for o in rivals)
        blocks.append(
            GateBlock(
                CONTESTED,
                f"{len(rivals)} other {noun} for this date with no room assigned yet ({listed}). "
                "Which one gets which room is a sales call.",
            )
        )

    return GateDecision(should_draft=not blocks, blocks=blocks, facts=facts)
