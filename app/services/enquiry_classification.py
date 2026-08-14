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

import datetime as dt
import logging
import re
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, BookingEvent, Space, Venue
from app.models.booking import BookingStatus
from app.services import ivvy_import
from app.services import notifications
from app.services.booking import create_booking
from app.services.contact_matching import DuplicateCandidate, find_or_create_contact
from app.utils import truncate

logger = logging.getLogger(__name__)

# One immediate retry on a transient SMTP failure (a dropped connection,
# a momentary Gmail hiccup) before giving up and leaving it for a human --
# see notify_new_enquiry. Short on purpose: this runs synchronously inside
# the enquiry submission request, so the client is waiting on it.
ENQUIRY_NOTIFICATION_RETRY_DELAY_SECONDS = 1.5
NOTIFICATION_FAILURE_REASON_MAX_LENGTH = 2000

GENERIC_EVENT_TYPES = {"party", "event", "other", "not sure yet", ""}

# A double-click (or a client/staff-member retrying a slow/ambiguous
# response) fires two near-identical submissions within a second or two
# of each other -- this window is intentionally short so a genuine second
# enquiry made minutes later is never mistaken for a duplicate.
DUPLICATE_SUBMISSION_WINDOW = dt.timedelta(seconds=15)

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


def _find_recent_duplicate(db: Session, *, contact_id, event_date, event_name) -> Booking | None:
    cutoff = dt.datetime.now(dt.timezone.utc) - DUPLICATE_SUBMISSION_WINDOW
    return db.execute(
        select(Booking)
        .where(
            Booking.contact_id == contact_id,
            Booking.event_date == event_date,
            Booking.event_name == event_name,
            Booking.created_at >= cutoff,
        )
        .order_by(Booking.created_at.desc())
    ).scalars().first()


def create_enquiry_booking(
    db: Session,
    *,
    venue: Venue,
    full_name: str,
    email: str,
    phone: str | None,
    event_name: str,
    event_type: str,
    event_date: dt.date | None,
    proposed_time_slot: str | None,
    attendee_count: int | None,
    adult_count: int | None,
    company_name: str | None,
    dates_flexible: bool,
    comments: str | None,
    lead_source: str,
    lead_referrer: str | None,
    actor: str,
) -> tuple[Booking, list[DuplicateCandidate], bool]:
    """The single path an enquiry becomes a Booking, regardless of whether
    a client submitted it themselves (app.api.enquiries) or staff entered
    it on their behalf (app.api.admin_bookings) -- a phone call, a direct
    email, or an iVvy marketplace lead all go through exactly the same
    contact-matching, duplicate-guard, and classify_and_flag logic a web
    form submission gets. Returns (booking, duplicate_candidates,
    is_new) -- is_new is False when a near-identical submission already
    exists within DUPLICATE_SUBMISSION_WINDOW and that existing booking
    was reused instead of creating a second one."""
    contact, duplicate_candidates = find_or_create_contact(db, full_name, email, phone)

    existing = _find_recent_duplicate(db, contact_id=contact.id, event_date=event_date, event_name=event_name)
    if existing is not None:
        return existing, duplicate_candidates, False

    unassigned_space_id = ivvy_import.get_unassigned_space_id(db, venue)

    notes_parts = []
    if company_name:
        notes_parts.append(f"Company: {company_name}")
    if dates_flexible:
        notes_parts.append("Dates flexible: yes")
    if comments:
        notes_parts.append(comments)
    notes = "\n".join(notes_parts) or None

    # Adult/child split: only known if adult_count was volunteered. Left
    # unknown, every attendee is conservatively treated as an adult for
    # minimum-spend purposes. If even the total guest count is unknown,
    # both are recorded as 0 (the model's own default) --
    # classify_and_flag raises a missing-guest-count flag so this is
    # never silent.
    if attendee_count is None:
        resolved_adult_count, resolved_child_count = 0, 0
    elif adult_count is not None:
        resolved_adult_count = adult_count
        resolved_child_count = attendee_count - adult_count
    else:
        resolved_adult_count = attendee_count
        resolved_child_count = 0

    booking = create_booking(
        db,
        space_id=unassigned_space_id,
        contact_id=contact.id,
        event_date=event_date,
        proposed_time_slot=proposed_time_slot,
        event_name=event_name,
        event_type=event_type,
        adult_count=resolved_adult_count,
        child_count=resolved_child_count,
        notes=notes,
        actor=actor,
        lead_source=lead_source,
        lead_referrer=lead_referrer,
    )

    flags = classify_and_flag(
        db,
        booking,
        event_type=event_type,
        adult_count=adult_count,
        attendee_count=attendee_count,
        actor=actor,
        possible_duplicate_contact=len(duplicate_candidates) > 0,
    )

    notify_new_enquiry(db, booking, flags=flags, actor=actor)

    return booking, duplicate_candidates, True


def notify_new_enquiry(db: Session, booking: Booking, *, flags: list[str], actor: str) -> None:
    """The one email that makes a brand-new enquiry visible outside this
    database -- see app.services.notifications' module docstring on why a
    client reply can only ever be drafted from what actually lands in the
    venue's own inbox. Tries once, retries once on a transient failure,
    and never raises: a failed send must not take the enquiry -- or the
    client's own thank-you page -- down with it. A failure that survives
    both attempts is instead written to the audit trail and left for a
    human to see and resend -- see get_enquiry_notification_failures and
    resend_enquiry_notification."""
    if not notifications.is_gmail_smtp_configured():
        logger.error(
            "Enquiry notification not sent for booking %s (%s): Gmail SMTP is not configured",
            booking.reference_code, booking.id,
        )
        _record_notification_failure(db, booking, reason="Gmail SMTP is not configured", actor=actor)
        return

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            notifications.send_enquiry_notification_email(
                booking, flags=flags, dashboard_base_url=settings.dashboard_base_url
            )
        except Exception as exc:  # noqa: BLE001 -- a live email-provider problem must not take the
            # enquiry submission down with it (same reasoning as the Stripe guard in app.api.invoices).
            last_error = exc
            logger.exception(
                "Enquiry notification email failed (attempt %s/2) for booking %s", attempt, booking.id
            )
            if attempt == 1:
                time.sleep(ENQUIRY_NOTIFICATION_RETRY_DELAY_SECONDS)
            continue
        _record_notification_success(db, booking, actor=actor)
        return

    _record_notification_failure(db, booking, reason=str(last_error), actor=actor)


def resend_enquiry_notification(db: Session, booking: Booking, *, actor: str) -> None:
    """The staff-facing recovery path once the automatic attempt above has
    exhausted its one retry. Re-derives flags from the booking's own audit
    trail (classify_and_flag already wrote them there) rather than
    re-running classification, so a resend reflects exactly what was
    originally flagged. Unlike notify_new_enquiry, this raises on failure
    -- the caller is a deliberate staff action, not a background step of
    enquiry submission, so a failure here should surface to whoever
    clicked the button, not just to the log."""
    flags = [e.new_value for e in booking.events if e.event_type == "enquiry_flagged" and e.new_value]
    try:
        notifications.send_enquiry_notification_email(
            booking, flags=flags, dashboard_base_url=settings.dashboard_base_url
        )
    except Exception as exc:
        logger.exception("Manual resend of the enquiry notification failed for booking %s", booking.id)
        _record_notification_failure(db, booking, reason=str(exc), actor=actor)
        raise
    _record_notification_success(db, booking, actor=actor)


def _record_notification_success(db: Session, booking: Booking, *, actor: str) -> None:
    booking.enquiry_notification_sent_at = dt.datetime.now(dt.timezone.utc)
    db.add(BookingEvent(booking_id=booking.id, event_type="enquiry_notification_sent", actor=actor))
    db.commit()
    db.refresh(booking)


def _record_notification_failure(db: Session, booking: Booking, *, reason: str, actor: str) -> None:
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="enquiry_notification_failed",
            new_value=truncate(reason, NOTIFICATION_FAILURE_REASON_MAX_LENGTH),
            actor=actor,
        )
    )
    db.commit()


def get_enquiry_notification_failures(db: Session, venue: Venue) -> list[Booking]:
    """Staff worklist: an enquiry notification that was attempted and has
    not (yet) succeeded. Self-clearing the moment it sends -- either the
    automatic retry inside notify_new_enquiry beats a transient failure,
    or staff use resend_enquiry_notification; either way
    enquiry_notification_sent_at gets set and this drops off. Gated on the
    presence of a genuine "enquiry_notification_failed" event, not merely
    on the timestamp being unset -- an iVvy import or a manually-placed
    hold never goes through notify_new_enquiry at all, and must never
    appear here just because they, too, have no sent_at."""
    failed = select(BookingEvent.booking_id).where(BookingEvent.event_type == "enquiry_notification_failed")
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Booking.enquiry_notification_sent_at.is_(None),
                Booking.id.in_(failed),
            )
            .order_by(Booking.event_date)
        ).all()
    )


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
            .order_by(Booking.event_date)
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
            .order_by(Booking.event_date)
        ).all()
    )
