import datetime as dt
import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import Booking, Space
from app.models.booking import BLOCKING_STATUSES


def is_space_free(db: Session, space_id: uuid.UUID, event_date: dt.date) -> tuple[bool, list[Booking]]:
    """Whole-day check: any booking that actually holds the space (see
    BLOCKING_STATUSES) for this space on this date?"""
    blocking = (
        db.execute(
            select(Booking)
            .where(
                Booking.space_id == space_id,
                Booking.event_date == event_date,
                Booking.status.in_(BLOCKING_STATUSES),
            )
            .order_by(Booking.start_time)
        )
        .scalars()
        .all()
    )
    return (len(blocking) == 0, blocking)


def get_space_candidates(
    db: Session,
    venue_id: uuid.UUID,
    event_date: dt.date,
    start_time: dt.time,
    end_time: dt.time,
    guest_count: int,
    *,
    require_wheelchair_accessible: bool = False,
) -> list[dict]:
    """Which spaces in this venue are free AND large enough for guest_count,
    in one query — this is called on every drafted reply, so no per-space
    round trip. Returns every space (including excluded ones) with
    why-not reasons, so the caller/LLM can explain the exclusion."""
    start_dt = dt.datetime.combine(event_date, start_time)
    end_dt = dt.datetime.combine(event_date, end_time)
    requested_range = func.tsrange(start_dt, end_dt, "[)")

    is_booked = exists(
        select(1).where(
            Booking.space_id == Space.id,
            Booking.status.in_(BLOCKING_STATUSES),
            Booking.time_range.op("&&")(requested_range),
        )
    )

    stmt = (
        select(Space, is_booked.label("is_booked"))
        .where(Space.venue_id == venue_id, Space.is_bookable.is_(True))
        .order_by(Space.name)
    )

    results = []
    for space, is_booked_flag in db.execute(stmt).all():
        reasons = []
        if space.capacity < guest_count:
            reasons.append("too_small")
        if is_booked_flag:
            reasons.append("already_booked")
        if require_wheelchair_accessible and not space.wheelchair_accessible:
            reasons.append("not_accessible")

        results.append(
            {
                "space": space,
                "is_available": len(reasons) == 0,
                "reasons": reasons,
            }
        )

    return results
