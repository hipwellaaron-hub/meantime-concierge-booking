"""The week-at-a-glance booking calendar. This is a view over data that
already exists -- Booking already carries space, date, times and status,
and a hold is just a Booking at 'tentative' status (see
app.services.booking.create_hold). Nothing here is a second source of
truth for what's booked: the double-booking guarantee still lives entirely
in the GIST exclusion constraint on the bookings table.

A cell can legitimately hold more than one entry (a Saturday daytime
function finishing at 5pm and an evening function starting after it, or
three separate live enquiries on the same date before any of them is
confirmed) -- callers must never collapse a cell to a single state.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Space, Venue
from app.models.booking import BLOCKING_STATUSES, BookingStatus

# What actually shows on the calendar. Built from BLOCKING_STATUSES (the
# same tuple the exclusion constraint and app.services.availability read)
# plus enquiry/offered, which are shown for visibility but never block --
# not a second, independently hand-typed list, so this can't silently fall
# out of sync if BLOCKING_STATUSES ever changes. cancelled/dead are
# deliberately excluded -- they're not live, and showing them would make a
# genuinely free room look occupied.
CALENDAR_STATUSES = (BookingStatus.enquiry, BookingStatus.offered) + BLOCKING_STATUSES

# Rendering categories. "hold_active" and "hold_expired" are both status
# 'tentative' underneath -- the split is purely a display distinction
# (see Booking.hold_expires_at), not a different blocking state, so an
# expired hold still counts as occupying the space exactly like an active
# one does under the exclusion constraint.
CalendarEntry = dict  # {"booking": Booking, "kind": str, "is_blocking": bool}


def week_start_for(any_date: dt.date) -> dt.date:
    """Monday of the week containing any_date."""
    return any_date - dt.timedelta(days=any_date.weekday())


def classify(booking: Booking, *, today: dt.date) -> str:
    if booking.status == BookingStatus.confirmed:
        return "confirmed"
    if booking.status == BookingStatus.completed:
        return "completed"
    if booking.status == BookingStatus.offered:
        return "offered"
    if booking.status == BookingStatus.enquiry:
        return "enquiry"
    if booking.status == BookingStatus.tentative:
        if booking.hold_expires_at is not None and booking.hold_expires_at < today:
            return "hold_expired"
        return "hold_active"
    raise ValueError(f"'{booking.status.value}' is not a calendar status -- filter with CALENDAR_STATUSES first")


def get_week_grid(db: Session, venue: Venue, week_start: dt.date, *, today: dt.date | None = None) -> dict:
    """week_start must be a Monday (see week_start_for). Returns bookable
    spaces in the venue, the 7 dates of the week, and cells nested as
    cells[space_id][date] -> list of {"booking": Booking, "kind": str},
    always present (possibly empty) for every space/date combination so
    templates never need a fallback lookup."""
    today = today or dt.date.today()
    week_end = week_start + dt.timedelta(days=6)

    spaces = list(
        db.scalars(
            select(Space).where(Space.venue_id == venue.id, Space.is_bookable.is_(True)).order_by(Space.name)
        ).all()
    )

    bookings = list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Space.is_bookable.is_(True),
                Booking.event_date >= week_start,
                Booking.event_date <= week_end,
                Booking.status.in_(CALENDAR_STATUSES),
            )
            .order_by(Booking.start_time.asc().nulls_first(), Booking.created_at)
        ).all()
    )

    days = [week_start + dt.timedelta(days=i) for i in range(7)]
    cells: dict[uuid.UUID, dict[dt.date, list[CalendarEntry]]] = {s.id: {d: [] for d in days} for s in spaces}
    for booking in bookings:
        space_cells = cells.get(booking.space_id)
        if space_cells is not None and booking.event_date in space_cells:
            space_cells[booking.event_date].append({
                "booking": booking,
                "kind": classify(booking, today=today),
                # Same BLOCKING_STATUSES the exclusion constraint and
                # is_space_free() read -- so "does this entry actually
                # hold the space" has one answer, not a kind-string list
                # re-derived at each call site (see the property test in
                # tests/test_calendar.py that checks this against
                # is_space_free directly).
                "is_blocking": booking.status in BLOCKING_STATUSES,
            })

    return {
        "spaces": spaces,
        "days": days,
        "cells": cells,
        "week_start": week_start,
        "week_end": week_end,
        "today": today,
    }


def get_undated_booking_count(db: Session, venue: Venue) -> int:
    """A booking with no event_date structurally cannot be placed on any
    day of a date grid -- it must not simply be missing with no trace. It's
    already surfaced in Triage's "awaiting space & time assignment"
    worklist (see app.services.ivvy_import.get_unassigned_bookings); this
    just gives the calendar page a count to point at rather than
    duplicating that worklist here."""
    return (
        db.query(Booking)
        .join(Space, Booking.space_id == Space.id)
        .filter(
            Space.venue_id == venue.id,
            Booking.event_date.is_(None),
            Booking.status.in_(CALENDAR_STATUSES),
        )
        .count()
    )
