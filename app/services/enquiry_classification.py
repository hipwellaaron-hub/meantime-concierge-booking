"""Staff-facing triage flags for freshly-captured enquiries.

Nothing here sends anything -- matching the project-wide rule that no
client-facing action ever auto-sends (see app.services.wizard_generation).
Every flag is a BookingEvent (event_type "enquiry_flagged") that surfaces
in the staff dashboard (booking detail banner + Triage worklist) for a
human to actually act on.

The birthday rule specifically: a generic "Birthday" selection tells you
nothing about whether 18th-birthday conditions (RSA, ID checks, parental
supervision) actually apply. The real trigger is whether underage guests
will be drinking on-site, not the dropdown label -- a 16th isn't an 18th
but still has underage guests; a 21st often has younger siblings in the
room. So a generic "Birthday" must never silently inherit or omit those
conditions -- it has to be confirmed first. And the milestone is
frequently only disclosed in the event name or the free-text comments,
never the dropdown itself, so both are searched, not just the Event Type
field.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Space, Venue
from app.models.booking import BookingStatus

GENERIC_EVENT_TYPES = {"party", "event", "other", "not sure yet", ""}

BIRTHDAY_CLARIFICATION_QUESTION = (
    "Just so I can set everything up properly, is this a milestone birthday, "
    "and will any guests be under 18?"
)

SATURDAY = 5
WEDNESDAY = 2  # dt.date.weekday(): Monday=0 ... Sunday=6
THURSDAY = 3

# `(th)?` makes the suffix optional ("18 birthday" is a real example, no
# "th"); the lookahead requires "birthday"/"bday"/"b'day" to actually
# follow, so a 21st mentioning "some guests under 18" doesn't false-positive.
_EIGHTEENTH_PATTERN = re.compile(r"\b18(th)?\b(?=[^a-z]*(birthday|bday|b'?day))")
_ACCESSIBILITY_PATTERN = re.compile(r"\blift\b|wheelchair|accessib|stairs|mobility")


def looks_like_18th(event_type: str, event_name: str, notes: str | None) -> bool:
    if event_type.strip().lower() == "18th birthday":
        return True
    haystack = f"{event_name} {notes or ''}".lower()
    return bool(_EIGHTEENTH_PATTERN.search(haystack)) or "eighteenth" in haystack


def classify_and_flag(
    db: Session,
    booking: Booking,
    *,
    event_type: str,
    adult_count: int | None,
    attendee_count: int | None,
    actor: str,
    possible_duplicate_contact: bool = False,
) -> list[str]:
    """Returns the flag notes raised (also persisted as BookingEvents)."""
    flags: list[str] = []
    event_type_normalized = event_type.strip().lower()

    if possible_duplicate_contact:
        flags.append(
            "This contact looks similar to an existing contact on file (name/email match) -- "
            "check before treating this as a brand-new client."
        )

    looks_18th = looks_like_18th(event_type, booking.event_name, booking.notes)

    if event_type_normalized == "birthday" and not looks_18th:
        flags.append(
            "Generic 'Birthday' enquiry -- milestone and guest ages not yet known. "
            "Do not apply 18th-birthday conditions (RSA, ID checks, parental supervision) until confirmed. "
            f'Ask: "{BIRTHDAY_CLARIFICATION_QUESTION}"'
        )

    if looks_18th:
        flags.append(
            "18th birthday confirmed (or as good as) -- RSA, ID checks, and parental supervision conditions apply."
        )
        if booking.event_date is not None and booking.event_date.weekday() == SATURDAY:
            flags.append(
                "18th birthday requested for a Saturday -- standard is Friday, Saturday only where "
                "the date is close and otherwise empty. Confirm with Aaron before proceeding."
            )

    if event_type_normalized in GENERIC_EVENT_TYPES:
        flags.append(f"Event type '{event_type}' is unclear -- confirm what kind of event this actually is.")

    if attendee_count is not None and adult_count is None:
        flags.append(
            "Adult/minor guest split not provided -- confirm before finalizing minimum spend "
            "(minimums count adults only)."
        )

    if attendee_count is None:
        flags.append(
            "Guest count not provided -- confirm before proceeding (minimum spend and space "
            "capacity both depend on it)."
        )

    if booking.event_date is None:
        flags.append("Event date not provided -- confirm before proceeding.")
    elif booking.event_date.weekday() in (WEDNESDAY, THURSDAY):
        day_name = booking.event_date.strftime("%A")
        flags.append(
            f"{day_name} requested -- trading closes at 9pm, confirm an evening function actually "
            "fits before promising the date."
        )

    notes_lower = (booking.notes or "").lower()
    if _ACCESSIBILITY_PATTERN.search(notes_lower):
        flags.append("Accessibility need raised in the enquiry -- confirm requirements before proceeding.")
        accessible_space = db.execute(
            select(Space).where(Space.venue_id == booking.space.venue_id, Space.wheelchair_accessible.is_(True))
        ).scalars().first()
        accessible_capacity = accessible_space.capacity if accessible_space is not None else 0
        if attendee_count is not None and attendee_count > accessible_capacity:
            flags.append(
                f"Guest count ({attendee_count}) exceeds the only wheelchair-accessible space's "
                f"capacity ({accessible_capacity}) -- may need to decline or offer an alternative."
            )

    if attendee_count is not None:
        largest = db.execute(
            select(func.max(Space.capacity)).where(
                Space.venue_id == booking.space.venue_id, Space.is_bookable.is_(True)
            )
        ).scalar_one()
        if largest is not None and attendee_count > largest:
            flags.append(
                f"Guest count ({attendee_count}) exceeds every single space's capacity "
                f"(largest is {largest}) -- would need to combine spaces or decline."
            )

    for note in flags:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="enquiry_flagged",
                field_name="birthday_classification",
                new_value=note,
                actor=actor,
            )
        )
    if flags:
        db.commit()
    return flags


def get_enquiries_needing_clarification(db: Session, venue: Venue) -> list[Booking]:
    """Staff worklist: still-open enquiries with at least one unresolved
    flag. Drops off once staff move the booking past 'enquiry' status --
    same self-clearing pattern as the other triage worklists (see
    app.services.ivvy_import.get_unassigned_bookings and
    app.services.wizard.get_wizard_eligible_bookings)."""
    flagged = select(BookingEvent.booking_id).where(BookingEvent.event_type == "enquiry_flagged")
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Booking.status == BookingStatus.enquiry,
                Booking.id.in_(flagged),
            )
            .order_by(Booking.created_at)
        ).all()
    )


def get_flagged_bookings_in_progress(db: Session, venue: Venue) -> list[Booking]:
    """Staff worklist: bookings flagged at enquiry time that have since
    moved past 'enquiry' status (so they've dropped off
    get_enquiries_needing_clarification above) but aren't yet at a terminal
    status. A flag never gets silently cleared just because the booking
    progressed -- e.g. an 18th-on-Saturday flag still needs Aaron's
    confirmation whether the booking is at 'enquiry' or 'tentative'."""
    flagged = select(BookingEvent.booking_id).where(BookingEvent.event_type == "enquiry_flagged")
    open_past_enquiry = (BookingStatus.offered, BookingStatus.tentative, BookingStatus.confirmed)
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Booking.status.in_(open_past_enquiry),
                Booking.id.in_(flagged),
            )
            .order_by(Booking.created_at)
        ).all()
    )
