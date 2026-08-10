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
conditions -- it has to be confirmed first.
"""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Space, Venue
from app.models.booking import BookingStatus

GENERIC_BIRTHDAY = "Birthday"
EIGHTEENTH_BIRTHDAY = "18th Birthday"
BIRTHDAY_EVENT_TYPES = {"18th Birthday", "21st Birthday", "Birthday"}

BIRTHDAY_CLARIFICATION_QUESTION = (
    "Just so I can set everything up properly, is this a milestone birthday, "
    "and will any guests be under 18?"
)

SATURDAY = 5  # dt.date.weekday(): Monday=0 ... Sunday=6


def classify_and_flag(
    db: Session,
    booking: Booking,
    *,
    event_type: str,
    adult_count: int | None,
    actor: str,
    possible_duplicate_contact: bool = False,
) -> list[str]:
    """Returns the flag notes raised (also persisted as BookingEvents)."""
    flags: list[str] = []

    if possible_duplicate_contact:
        flags.append(
            "This contact looks similar to an existing contact on file (name/email match) -- "
            "check before treating this as a brand-new client."
        )

    if event_type == GENERIC_BIRTHDAY:
        flags.append(
            "Generic 'Birthday' enquiry -- milestone and guest ages not yet known. "
            "Do not apply 18th-birthday conditions (RSA, ID checks, parental supervision) until confirmed. "
            f'Ask: "{BIRTHDAY_CLARIFICATION_QUESTION}"'
        )

    if event_type in BIRTHDAY_EVENT_TYPES and adult_count is None:
        flags.append(
            "Adult/minor guest split not provided -- confirm before finalizing minimum spend "
            "(minimums count adults only)."
        )

    if event_type == EIGHTEENTH_BIRTHDAY and booking.event_date.weekday() == SATURDAY:
        flags.append(
            "18th birthday requested for a Saturday -- standard is Friday, Saturday only where "
            "the date is close and otherwise empty. Confirm with Aaron before proceeding."
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
