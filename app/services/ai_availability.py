"""Availability as the AI needs to see it (brief section 3.2).

Different from app.services.availability in one decisive way: that module
answers "can I book this?", so it reads BLOCKING_STATUSES and an enquiry is
invisible to it. This one answers "what is touching this slot?", which
includes every open enquiry -- because "free" and "nobody else is asking"
are different facts, and it was the second one that was being tracked in
somebody's head. Saturday 17 October had six parties on the Loft.

INDEPENDENCE NOTE. The acceptance test (brief section 12) cross-checks this
endpoint against bookings-by-date across ninety days. That test is only
worth running if the two are computed by genuinely different paths -- if
they shared a query they would agree by construction and prove nothing.
So this module groups per space per date from its own query, while
bookings_on_date() below is a flat select the booking endpoint uses. Keep
them separate. Making one call the other would silently turn the
acceptance test into decoration.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Booking, Space
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType

# Everything that touches a slot, not merely what blocks it.
TOUCHING_STATUSES = (
    BookingStatus.enquiry,
    BookingStatus.offered,
    BookingStatus.tentative,
    BookingStatus.confirmed,
    BookingStatus.completed,
)

# How a booking is bucketed in the availability response.
CONFIRMED_STATUSES = (BookingStatus.confirmed, BookingStatus.completed)
TENTATIVE_STATUSES = (BookingStatus.tentative,)
OPEN_ENQUIRY_STATUSES = (BookingStatus.enquiry, BookingStatus.offered)


def day_of_week(value: dt.date) -> str:
    """Named here rather than formatted at the edge, because the whole
    point is that the AI can check a client's "Saturday 21 November"
    against the date that was actually written down."""
    return value.strftime("%A")


def spaces_for(db: Session, venue) -> list[Space]:
    return list(
        db.scalars(
            select(Space)
            .where(Space.venue_id == venue.id, Space.is_bookable.is_(True))
            .order_by(Space.name)
        ).all()
    )


def _agreement_state(booking: Booking) -> tuple[bool, bool]:
    signed = any(
        d.type == DocumentType.agreement and d.is_current and d.status == DocumentStatus.signed
        for d in booking.documents
    )
    paid = any(
        i.type == InvoiceType.deposit and i.status == InvoiceStatus.paid for i in booking.invoices
    )
    return signed, paid


def load_touching(
    db: Session, venue, *, date_from: dt.date, date_to: dt.date, space_id: uuid.UUID | None = None
) -> list[Booking]:
    """Own query path -- see the independence note at the top of this
    module. Loads every booking touching the window, including linked
    children, because a child genuinely occupies its own room."""
    stmt = (
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == venue.id,
            Booking.event_date >= date_from,
            Booking.event_date <= date_to,
            Booking.status.in_(TOUCHING_STATUSES),
        )
        .options(
            selectinload(Booking.documents),
            selectinload(Booking.invoices),
            selectinload(Booking.contact),
            selectinload(Booking.space),
        )
        .order_by(Booking.event_date, Booking.start_time)
    )
    if space_id is not None:
        stmt = stmt.where(Booking.space_id == space_id)
    return list(db.scalars(stmt).all())


def _time_range(booking: Booking) -> dict | None:
    if booking.start_time is None or booking.end_time is None:
        return None
    return {
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
    }


def build_availability(
    db: Session,
    venue,
    *,
    date_from: dt.date,
    date_to: dt.date,
    space_id: uuid.UUID | None = None,
    today: dt.date | None = None,
) -> list[dict]:
    """Per date, per space: everything touching the slot.

    Time-aware throughout, matching the exclusion constraint, so a lunch
    and an evening in the same room both appear and neither hides the
    other.
    """
    today = today or dt.date.today()
    spaces = spaces_for(db, venue)
    if space_id is not None:
        spaces = [s for s in spaces if s.id == space_id]

    touching = load_touching(
        db, venue, date_from=date_from, date_to=date_to, space_id=space_id
    )

    grouped: dict[tuple, list[Booking]] = {}
    for booking in touching:
        grouped.setdefault((booking.event_date, booking.space_id), []).append(booking)

    days = []
    cursor = date_from
    while cursor <= date_to:
        space_blocks = []
        for space in spaces:
            here = grouped.get((cursor, space.id), [])

            confirmed, tentative, open_enquiries = [], [], []
            for booking in here:
                entry = {
                    "reference": booking.reference_code,
                    "id": str(booking.id),
                    "event_name": booking.event_name,
                    **( _time_range(booking) or {"start_time": None, "end_time": None} ),
                }
                if booking.status in CONFIRMED_STATUSES:
                    confirmed.append(entry)
                elif booking.status in TENTATIVE_STATUSES:
                    signed, paid = _agreement_state(booking)
                    entry.update(
                        {
                            "signed": signed,
                            "paid": paid,
                            "hold_expires_at": booking.hold_expires_at.isoformat()
                            if booking.hold_expires_at
                            else None,
                            "days_since_offered": (today - booking.hold_expires_at).days
                            if booking.hold_expires_at
                            else None,
                        }
                    )
                    tentative.append(entry)
                elif booking.status in OPEN_ENQUIRY_STATUSES:
                    entry.update(
                        {
                            "status": booking.status.value,
                            "contact_name": booking.contact.name if booking.contact else None,
                            "adults": booking.adult_count,
                        }
                    )
                    open_enquiries.append(entry)

            space_blocks.append(
                {
                    "space": space.name,
                    "space_id": str(space.id),
                    "confirmed": confirmed,
                    "tentative": tentative,
                    "open_enquiries": open_enquiries,
                    # Tier 2 proposals are a later step in the build order.
                    # The key exists from day one so the response shape does
                    # not change when they land.
                    "pending_proposals": [],
                    "contested": bool(tentative or open_enquiries or len(confirmed) > 1),
                }
            )

        days.append(
            {
                "date": cursor.isoformat(),
                "day_of_week": day_of_week(cursor),
                "spaces": space_blocks,
            }
        )
        cursor += dt.timedelta(days=1)

    return days


def bookings_on_date(
    db: Session, venue, *, on: dt.date, space_id: uuid.UUID | None = None
) -> list[Booking]:
    """The SECOND, independent path (brief section 10a.2).

    A flat select with no grouping and no per-space iteration -- used by
    the bookings endpoint and by the ninety-day cross-check. If this and
    build_availability ever disagree about what occupies a slot, that
    disagreement is exactly what the acceptance test is for.
    """
    stmt = (
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == venue.id,
            Booking.event_date == on,
            Booking.status.in_(TOUCHING_STATUSES),
        )
        .options(selectinload(Booking.space), selectinload(Booking.contact))
        .order_by(Booking.start_time)
    )
    if space_id is not None:
        stmt = stmt.where(Booking.space_id == space_id)
    return list(db.scalars(stmt).all())
