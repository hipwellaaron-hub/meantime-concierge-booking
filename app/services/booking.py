"""Booking creation and status transitions. Every write here also appends
to booking_events — that's the whole point of the audit log: state changes
must never happen without leaving a trace of who/what/when/old->new.
"""

import datetime as dt
import logging
import secrets
import string
import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Contact, Document, Invoice, Space
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType

logger = logging.getLogger(__name__)

REFERENCE_ALPHABET = string.ascii_uppercase + string.digits
REFERENCE_SUFFIX_LENGTH = 5

# Full-day span used when a hold is created with no specific time. A NULL
# start/end time produces a NULL time_range (see app/models/booking.py's
# comment on the Computed column), and a NULL time_range never conflicts
# under the exclusion constraint -- so an untimed "block the whole day"
# hold would look correctly occupied on the calendar without actually
# being protected against a real double-booking. Pinning it to a concrete
# span keeps it genuinely behind the same database constraint that
# protects every other blocking booking. 11:30pm matches the existing
# music-off-time boundary (app.services.validation.MUSIC_OFF_TIME) as the
# latest reasonable end of day.
HOLD_FULL_DAY_START = dt.time(9, 0)
HOLD_FULL_DAY_END = dt.time(23, 30)

# The booking lifecycle. Terminal statuses (completed, cancelled, dead) map
# to an empty tuple -- no further transition is legal from there. This is
# enforced only by transition_status() below, not by change_status() itself:
# change_status() stays an unchecked primitive because existing one-off
# scripts and test fixtures already jump straight to a status (e.g.
# enquiry -> confirmed) for setup convenience, and must keep working.
LEGAL_TRANSITIONS: dict[BookingStatus, tuple[BookingStatus, ...]] = {
    BookingStatus.enquiry: (BookingStatus.offered, BookingStatus.dead),
    BookingStatus.offered: (BookingStatus.tentative, BookingStatus.dead),
    BookingStatus.tentative: (BookingStatus.confirmed, BookingStatus.cancelled),
    BookingStatus.confirmed: (BookingStatus.completed, BookingStatus.cancelled),
    BookingStatus.completed: (),
    BookingStatus.cancelled: (),
    BookingStatus.dead: (),
    # No entry allows a transition *into* archived from here -- it's
    # deliberately unreachable via the normal staff status dropdown (see
    # the enum's own comment on Booking.status). Still needs an entry so
    # an already-archived booking correctly reports no further legal
    # transition, same as any other terminal status.
    BookingStatus.archived: (),
}


def generate_reference_code(db: Session, event_date: dt.date | None, venue_slug: str = "HAM") -> str:
    """Human-readable and unique. Retries on the rare random collision
    rather than relying on any external counter. event_date is None for
    an enquiry that arrived with no date locked in yet -- "TBD" stands in
    for the date segment rather than guessing one."""
    date_part = f"{event_date:%Y%m%d}" if event_date is not None else "TBD"
    for _ in range(10):
        suffix = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_SUFFIX_LENGTH))
        code = f"{venue_slug.upper()}-{date_part}-{suffix}"
        exists = db.execute(select(Booking.id).where(Booking.reference_code == code)).first()
        if exists is None:
            return code
    raise RuntimeError("Could not generate a unique reference code after 10 attempts")


def create_booking(
    db: Session,
    *,
    space_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    event_date: dt.date | None,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    proposed_time_slot: str | None = None,
    event_name: str,
    event_type: str | None,
    adult_count: int,
    child_count: int,
    notes: str | None,
    actor: str,
    status: BookingStatus = BookingStatus.enquiry,
    lead_source: str | None = None,
    lead_referrer: str | None = None,
    first_touch_attribution: dict | None = None,
    last_touch_attribution: dict | None = None,
    migration_source: str | None = None,
    migration_external_ref: str | None = None,
    migration_snapshot: dict | None = None,
    agreed_min_adults: int | None = None,
    pricing_locked_at: dt.date | None = None,
) -> Booking:
    space = db.get(Space, space_id)
    if space is None:
        raise ValueError(f"Unknown space {space_id}")

    booking = Booking(
        space_id=space_id,
        contact_id=contact_id,
        event_date=event_date,
        start_time=start_time,
        end_time=end_time,
        proposed_time_slot=proposed_time_slot,
        event_name=event_name,
        event_type=event_type,
        adult_count=adult_count,
        child_count=child_count,
        notes=notes,
        status=status,
        reference_code=generate_reference_code(db, event_date),
        lead_source=lead_source,
        lead_referrer=lead_referrer,
        first_touch_attribution=first_touch_attribution,
        last_touch_attribution=last_touch_attribution,
        migration_source=migration_source,
        migration_external_ref=migration_external_ref,
        migration_snapshot=migration_snapshot,
        # Defaults to the space's standard minimum -- "the agreed minimum
        # defaults to the standard" (Master Policy v1.3 §4.1). Only ever
        # changes from here via an explicit staff reduction afterward.
        agreed_min_adults=agreed_min_adults if agreed_min_adults is not None else space.standard_min_adults,
        # Defaults to today -- correct for the normal enquiry/booking flow,
        # where created_at and the real quote date are the same day. A
        # caller importing a booking from a prior system with no reliable
        # "quoted on" date in its export gets this same default, which
        # matches today's existing (created_at-based) behavior exactly.
        pricing_locked_at=pricing_locked_at if pricing_locked_at is not None else dt.date.today(),
    )
    db.add(booking)
    db.flush()  # assigns booking.id, and is where the exclusion constraint fires

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="created",
            actor=actor,
            new_value=status.value,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def change_status(
    db: Session, booking: Booking, new_status: BookingStatus, *, actor: str, reason: str | None = None
) -> Booking:
    """Unchecked primitive -- sets status and logs it, no legality check.
    Staff-facing transitions must go through transition_status() instead,
    which validates against LEGAL_TRANSITIONS before calling this."""
    old_status = booking.status
    if old_status == new_status:
        return booking

    booking.status = new_status
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="status_changed",
            field_name="status",
            old_value=old_status.value,
            new_value=new_status.value,
            actor=actor,
        )
    )
    if reason:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="status_changed",
                field_name="status_change_reason",
                new_value=reason,
                actor=actor,
            )
        )
    db.commit()
    db.refresh(booking)
    return booking


TERMINAL_STATUSES = (BookingStatus.completed, BookingStatus.cancelled, BookingStatus.dead, BookingStatus.archived)


def transition_status(
    db: Session, booking: Booking, new_status: BookingStatus, *, actor: str, reason: str | None = None
) -> Booking:
    """The validated, staff-facing entry point for moving a booking through
    its lifecycle. Enforces LEGAL_TRANSITIONS; raises ValueError (-> 422 at
    the API layer) on an illegal move rather than allowing it silently.
    Re-submitting the current status is treated as a harmless no-op (e.g. a
    double form submission) rather than an illegal transition.

    If this booking is a linked-spaces parent (see add_linked_space), every
    still-active linked child moves with it -- they're the same real event,
    just occupying a second room, so cancelling/confirming/completing the
    event must free or hold every room it touches, not just this one. A
    child already ended independently (e.g. that room was released early
    while the event carried on elsewhere) is left alone. Transitioning a
    *child* directly does not cascade back up or sideways -- only the
    parent's own move propagates."""
    if new_status == booking.status:
        result = change_status(db, booking, new_status, actor=actor, reason=reason)
    else:
        legal = LEGAL_TRANSITIONS.get(booking.status, ())
        if new_status not in legal:
            allowed = ", ".join(s.value for s in legal) or "none -- this is a terminal status"
            raise ValueError(f"Cannot move from '{booking.status.value}' to '{new_status.value}'. Allowed: {allowed}.")
        result = change_status(db, booking, new_status, actor=actor, reason=reason)

    # Queried directly rather than via booking.linked_bookings: the
    # session here has expire_on_commit=False (see tests/conftest.py), so
    # a relationship collection accessed earlier in the same session
    # (e.g. right after add_linked_space created a child) would otherwise
    # keep returning its stale value instead of the child just created.
    children = db.scalars(select(Booking).where(Booking.parent_booking_id == booking.id)).all()
    for child in children:
        if child.status not in TERMINAL_STATUSES:
            change_status(db, child, new_status, actor=actor, reason=reason)
    return result


# The two things that together mean a booking is genuinely won. Kept as
# one-booking predicates here; app.services.wizard.get_wizard_eligible_bookings
# expresses the same rule as bulk IN-subqueries because it answers a
# different shape of question (a whole worklist, not one booking).
# tests/test_auto_confirm.py asserts the two agree, so they can't drift.


def has_paid_deposit(db: Session, booking: Booking) -> bool:
    return (
        db.execute(
            select(Invoice.id).where(
                Invoice.booking_id == booking.id,
                Invoice.type == InvoiceType.deposit,
                Invoice.status == InvoiceStatus.paid,
            )
        ).first()
        is not None
    )


def has_signed_agreement(db: Session, booking: Booking) -> bool:
    """The *current* agreement specifically: regenerating an agreement
    supersedes the signed one (is_current flips to False), and what the
    client signed no longer describes the deal on offer."""
    return (
        db.execute(
            select(Document.id).where(
                Document.booking_id == booking.id,
                Document.type == DocumentType.agreement,
                Document.status == DocumentStatus.signed,
                Document.is_current.is_(True),
            )
        ).first()
        is not None
    )


# The statuses an automatic confirmation may move a booking out of.
# Everything else is either already confirmed (nothing to do) or terminal
# -- and a terminal booking must never be resurrected by a late payment
# or a stray signature. See AUTO_CONFIRM_REASON's use below.
AUTO_CONFIRMABLE_STATUSES = (BookingStatus.enquiry, BookingStatus.offered, BookingStatus.tentative)

AUTO_CONFIRM_REASON = "deposit paid and agreement signed"


def auto_confirm_if_ready(db: Session, booking: Booking, *, actor: str) -> bool:
    """Move a booking to confirmed once the client has both signed the
    current agreement and paid the deposit. Returns whether it did.

    Called after a signature and after a deposit payment, so whichever of
    the two lands second is the one that confirms -- order doesn't matter.

    Deliberately uses change_status rather than transition_status:
    LEGAL_TRANSITIONS models the staff dropdown (enquiry -> offered ->
    tentative -> confirmed), and it exists so staff don't skip steps by
    hand. A client who has signed the contract and paid the deposit is
    confirmed regardless of which stage staff had them sitting at, so
    this states that directly instead of loosening LEGAL_TRANSITIONS and
    thereby also loosening the staff-facing dropdown. The reason recorded
    on the audit event says why the jump was allowed.
    """
    if booking.parent_booking_id is not None:
        return False  # a linked child is only a second room; the parent owns the documents and invoices
    if booking.status not in AUTO_CONFIRMABLE_STATUSES:
        return False  # already confirmed, or terminal -- never resurrect a cancelled/archived booking
    if not (has_signed_agreement(db, booking) and has_paid_deposit(db, booking)):
        return False

    space = db.get(Space, booking.space_id)
    if space is None or not space.is_bookable:
        # Still in the "Unassigned (pending triage)" placeholder: there is
        # no real room yet, so "confirmed" would mean nothing, and would
        # make a placeholder space start blocking real time against every
        # other unassigned booking. Surfaced for a human instead of
        # silently skipped -- this booking is now MORE urgent, not less.
        flag_for_review(
            db,
            booking,
            note="Deposit paid and agreement signed, but no real space is assigned yet -- assign a space to confirm.",
            actor=actor,
        )
        return False

    try:
        change_status(db, booking, BookingStatus.confirmed, actor=actor, reason=AUTO_CONFIRM_REASON)
    except IntegrityError:
        # The exclusion constraint refused it: confirming makes a booking
        # blocking (see BLOCKING_STATUSES), and this one overlaps a
        # booking already holding the room. That's the constraint working,
        # not a bug -- but it must not take down the client's signing
        # request or a Stripe webhook. The signature and payment were
        # committed before this ran and survive the rollback.
        db.rollback()
        booking = db.get(Booking, booking.id)
        logger.exception("Auto-confirm collided with an existing booking for %s", booking.reference_code)
        flag_for_review(
            db,
            booking,
            note=(
                "Deposit paid and agreement signed, but this booking's space and time clash with another "
                "confirmed booking, so it could not be confirmed automatically. Resolve the clash."
            ),
            actor=actor,
        )
        return False

    # Same cascade transition_status does: one event using two rooms must
    # hold both, not just the parent's.
    for child in db.scalars(select(Booking).where(Booking.parent_booking_id == booking.id)).all():
        if child.status not in TERMINAL_STATUSES:
            change_status(db, child, BookingStatus.confirmed, actor=actor, reason=AUTO_CONFIRM_REASON)
    return True


def search_bookings(
    db: Session, venue_id: uuid.UUID, *, status: BookingStatus | None = None, query: str | None = None, limit: int = 100
) -> list[Booking]:
    stmt = select(Booking).join(Space, Booking.space_id == Space.id).where(Space.venue_id == venue_id)
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if query and query.strip():
        like = f"%{query.strip()}%"
        stmt = stmt.outerjoin(Contact, Booking.contact_id == Contact.id).where(
            or_(Booking.event_name.ilike(like), Booking.reference_code.ilike(like), Contact.name.ilike(like))
        )
    # Soonest event first -- Postgres' default ASC null ordering already
    # puts a booking with no event_date yet (see event_date's own nullable
    # comment) at the end rather than the top, where it would misleadingly
    # look most urgent.
    return list(db.scalars(stmt.order_by(Booking.event_date).limit(limit)).all())


def confirm_setup_access(db: Session, booking: Booking, *, actor: str) -> Booking:
    """Master Policy v1.3 §1.8: setup access earlier than the 2pm standard
    'must be confirmed, never promised' -- this is that confirmation,
    staff-only. setup_access_confirmed is tri-state (see app/models/booking.py):
    False means a genuine pending request, so this only makes sense there."""
    if booking.setup_access_confirmed is not False:
        raise ValueError("no pending early setup-access request to confirm")
    booking.setup_access_confirmed = True
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="setup_access_confirmed",
            old_value="False",
            new_value="True",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def set_agreed_minimum(
    db: Session, booking: Booking, *, agreed_min_adults: int, reason: MinReductionReasonCode | None, actor: str
) -> Booking:
    """Master Policy v1.3 §4: 'changes only on Aaron's explicit approval...
    logged with who, when, and why... never set silently.' A reason is
    required whenever this differs from the space's own standard minimum
    (Space.standard_min_adults, never mutated here) and cleared once it's
    reset back to standard, so a stale reason never lingers."""
    if agreed_min_adults != booking.space.standard_min_adults and reason is None:
        raise ValueError("a reason is required when the agreed minimum differs from the space standard")
    if agreed_min_adults == booking.space.standard_min_adults:
        reason = None

    old_min, old_reason = booking.agreed_min_adults, booking.agreed_min_reduction_reason
    booking.agreed_min_adults = agreed_min_adults
    booking.agreed_min_reduction_reason = reason
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="agreed_min_adults",
            old_value=str(old_min),
            new_value=str(agreed_min_adults),
            actor=actor,
        )
    )
    if reason != old_reason:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="agreed_min_reduction_reason",
                old_value=old_reason.value if old_reason else None,
                new_value=reason.value if reason else None,
                actor=actor,
            )
        )
    db.commit()
    db.refresh(booking)
    return booking


def assign_space_and_time(
    db: Session,
    booking: Booking,
    *,
    space_id: uuid.UUID,
    start_time: dt.time,
    end_time: dt.time,
    event_date: dt.date | None = None,
    actor: str,
) -> Booking:
    """Triage action for iVvy-imported bookings, which land in the
    placeholder Unassigned space with no time-of-day (see
    app/services/ivvy_import.py). Moving to a real, bookable space is what
    actually subjects the booking to the double-booking exclusion
    constraint for the first time -- a genuine overlap raises
    IntegrityError, which the caller (app/api/admin_bookings.py) turns
    into a 409, same as any other constraint violation in this app.

    event_date is optional and only touched when given: an enquiry that
    arrived with a real date needs it left alone here, while one that
    arrived with no date (see app.services.enquiry_classification's
    missing_event_date flag) needs a place for staff to record it once
    they've actually spoken to the client -- this is that place, reusing
    the same triage action rather than adding a whole separate one.

    Also reconciles agreed_min_adults when it's still sitting at the old
    space's standard with no reduction reason recorded -- i.e. it was never
    deliberately touched. The placeholder Unassigned space always has
    standard_min_adults=0, so every freshly-imported booking starts there;
    left alone, moving it into a real space would leave a stale 0 with no
    reason, which looks exactly like an unrecorded silent reduction (see
    get_bookings_with_unrecorded_minimum_reduction). A genuinely
    staff-set custom minimum (one that already differs from the old
    space's standard) is left untouched."""
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")

    old_space = booking.space
    old_space_id, old_start, old_end = booking.space_id, booking.start_time, booking.end_time
    if (
        space_id != old_space_id
        and booking.agreed_min_adults == old_space.standard_min_adults
        and booking.agreed_min_reduction_reason is None
        and space.standard_min_adults != booking.agreed_min_adults
    ):
        old_min = booking.agreed_min_adults
        booking.agreed_min_adults = space.standard_min_adults
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="agreed_min_adults",
                old_value=str(old_min),
                new_value=str(space.standard_min_adults),
                actor=actor,
            )
        )
    booking.space_id = space_id
    booking.start_time = start_time
    booking.end_time = end_time
    if event_date is not None and event_date != booking.event_date:
        old_event_date = booking.event_date
        booking.event_date = event_date
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="event_date",
                old_value=str(old_event_date) if old_event_date else None,
                new_value=str(event_date),
                actor=actor,
            )
        )
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="space_id",
            old_value=str(old_space_id),
            new_value=str(space_id),
            actor=actor,
        )
    )
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="start_time/end_time",
            old_value=f"{old_start}-{old_end}",
            new_value=f"{start_time}-{end_time}",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)

    # Assigning a real space can be the last thing standing between a
    # booking and confirmation: auto_confirm_if_ready refuses while a
    # booking is still in the non-bookable placeholder, so a client who
    # signed and paid before their room was decided would otherwise stay
    # at enquiry forever with nothing left to trigger a re-check.
    auto_confirm_if_ready(db, booking, actor=actor)
    return booking


def set_outside_cake_permitted(db: Session, booking: Booking, *, permitted: bool, actor: str) -> Booking:
    """Staff-only grandfathering per Master Policy v1.3 §1.6 -- new
    bookings default False; only Aaron can flip this for an existing
    client he's already agreed with."""
    old = booking.outside_cake_permitted
    if old == permitted:
        return booking
    booking.outside_cake_permitted = permitted
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="outside_cake_permitted",
            old_value=str(old),
            new_value=str(permitted),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def set_contact(db: Session, booking: Booking, *, contact_id: uuid.UUID, actor: str) -> Booking:
    """Attaches or replaces a booking's contact -- the gap
    app.services.ivvy_calendar_import documents by design: a booking
    imported with real space/time but no email at all (that export has
    no email column) can only get a real contact once staff track one
    down, often from a live email thread rather than iVvy itself. Every
    client-facing send path already refuses without a contact with a
    valid email on file, so nothing goes out until this has actually run.

    Logs both the old and new contact id either way, including when
    there was no previous contact -- silently swapping who a booking's
    documents and invoices are addressed to should never be an
    unannounced change."""
    old_contact_id = booking.contact_id
    if old_contact_id == contact_id:
        return booking
    booking.contact_id = contact_id
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="contact_id",
            old_value=str(old_contact_id) if old_contact_id else None,
            new_value=str(contact_id),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def flag_for_review(db: Session, booking: Booking, *, note: str, actor: str) -> Booking:
    """Surfaces `note` on the booking's own "needs clarification" banner
    and the Triage "flagged bookings" worklist -- the same
    "enquiry_flagged" marker app.services.enquiry_classification.
    classify_and_flag already writes, reused here for anything else that
    needs a human to check before proceeding, regardless of how the
    booking arrived."""
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="enquiry_flagged",
            field_name="manual_review",
            new_value=note,
            actor=actor,
        )
    )
    db.commit()
    return booking


def get_bookings_with_unrecorded_minimum_reduction(db: Session, venue_id: uuid.UUID) -> list[Booking]:
    """Master Policy v1.3 SS4.4, enforceable version: agreed_min_adults
    should never differ from the space's own standard without a recorded
    reason. set_agreed_minimum() enforces this for every change made
    through the dashboard, but create_booking() accepts an explicit
    agreed_min_adults with no reason check (imports, migration, scripts) --
    this surfaces that drift for staff to close out rather than silently
    charging a shortfall nobody agreed to."""
    open_statuses = (BookingStatus.enquiry, BookingStatus.offered, BookingStatus.tentative, BookingStatus.confirmed)
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue_id,
                Booking.agreed_min_adults != Space.standard_min_adults,
                Booking.agreed_min_reduction_reason.is_(None),
                Booking.status.in_(open_statuses),
            )
            .order_by(Booking.event_date)
        ).all()
    )


def create_hold(
    db: Session,
    *,
    space_id: uuid.UUID,
    event_date: dt.date,
    event_name: str,
    contact_id: uuid.UUID | None = None,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    hold_expires_at: dt.date | None = None,
    actor: str,
) -> Booking:
    """A hold is a Booking created directly at 'tentative' status --
    skipping the normal enquiry -> offered pipeline entirely, for the
    "block this date for a client, no formal enquiry exists yet" case.
    Gets the exact same double-booking protection as any other tentative
    booking (see LEGAL_TRANSITIONS and the exclusion constraint in
    app/models/booking.py) because it IS one, not a parallel concept.
    hold_expires_at is optional -- a hold with none is deliberately
    open-ended, not a bug."""
    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")

    booking = create_booking(
        db,
        space_id=space_id,
        contact_id=contact_id,
        event_date=event_date,
        start_time=start_time if start_time is not None else HOLD_FULL_DAY_START,
        end_time=end_time if end_time is not None else HOLD_FULL_DAY_END,
        event_name=event_name,
        event_type=None,
        adult_count=0,
        child_count=0,
        notes=None,
        actor=actor,
        status=BookingStatus.tentative,
    )
    if hold_expires_at is not None:
        booking.hold_expires_at = hold_expires_at
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="field_changed",
                field_name="hold_expires_at",
                new_value=str(hold_expires_at),
                actor=actor,
            )
        )
        db.commit()
        db.refresh(booking)
    return booking


def set_hold_expiry(db: Session, booking: Booking, *, hold_expires_at: dt.date | None, actor: str) -> Booking:
    old = booking.hold_expires_at
    if old == hold_expires_at:
        return booking
    booking.hold_expires_at = hold_expires_at
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="field_changed",
            field_name="hold_expires_at",
            old_value=str(old) if old else None,
            new_value=str(hold_expires_at) if hold_expires_at else None,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(booking)
    return booking


def add_linked_space(
    db: Session,
    parent: Booking,
    *,
    space_id: uuid.UUID,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    actor: str,
) -> Booking:
    """A real event that needs two physical spaces at once (see the iVvy
    reconciliation's "multi-space" cases) is modelled as a second real
    Booking row linked to the first, not a pseudo Space that would lie
    about capacity/minimums for a room that doesn't actually exist. The
    parent alone carries the contact, documents, invoices, and wizard
    session -- see the guards in app.services.documents/invoicing/wizard
    that refuse to create any of those on a linked child directly.

    Mirrors the parent's event_date/event_name/event_type/status/contact
    onto the child so it reads as "the same event" everywhere it's shown
    (the calendar, triage, the audit trail), and defaults times to the
    parent's own -- pass explicit start_time/end_time when the second room
    is used for a different window (e.g. an afterparty space that opens
    later than the main room)."""
    if parent.parent_booking_id is not None:
        raise ValueError("cannot link a space to a booking that is itself a linked child -- link it to the parent")

    space = db.get(Space, space_id)
    if space is None or not space.is_bookable:
        raise ValueError(f"Unknown or non-bookable space {space_id}")
    # Queried directly rather than read off parent.linked_bookings: the
    # session here has expire_on_commit=False (see tests/conftest.py), so
    # a relationship collection already accessed once (e.g. by an earlier
    # call in the same request/test) would otherwise keep returning the
    # stale value it had at first load, silently letting the same space
    # be linked twice.
    existing_child_space_ids = set(
        db.execute(select(Booking.space_id).where(Booking.parent_booking_id == parent.id)).scalars().all()
    )
    if space_id == parent.space_id or space_id in existing_child_space_ids:
        raise ValueError("this space is already part of this booking")

    child = create_booking(
        db,
        space_id=space_id,
        contact_id=parent.contact_id,
        event_date=parent.event_date,
        start_time=start_time if start_time is not None else parent.start_time,
        end_time=end_time if end_time is not None else parent.end_time,
        event_name=parent.event_name,
        event_type=parent.event_type,
        adult_count=parent.adult_count,
        child_count=parent.child_count,
        notes=None,
        actor=actor,
        status=parent.status,
    )
    child.parent_booking_id = parent.id
    db.add(
        BookingEvent(
            booking_id=parent.id,
            event_type="linked_space_added",
            field_name="linked_bookings",
            new_value=f"{space.name} ({child.reference_code})",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(child)
    return child
