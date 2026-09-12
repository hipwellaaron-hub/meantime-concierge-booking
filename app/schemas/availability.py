import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel


class BlockingBooking(BaseModel):
    reference_code: str
    status: str
    # Nullable: a booking can hold the space (e.g. a migrated 'confirmed'
    # import) without a pinned-down time-of-day yet -- see Booking model.
    start_time: dt.time | None
    end_time: dt.time | None


class SpaceFreeResponse(BaseModel):
    space_id: uuid.UUID
    space_name: str
    event_date: dt.date
    is_free: bool
    blocking_bookings: list[BlockingBooking]


class SpaceCandidate(BaseModel):
    space_id: uuid.UUID
    space_name: str
    capacity: int
    min_food_spend: Decimal
    wheelchair_accessible: bool
    is_available: bool
    reasons: list[str]


class SpaceAvailabilityResponse(BaseModel):
    # WHICH VENUE THIS ANSWER IS ABOUT. A list of rooms, capacities and
    # minimum spends is a quote in all but name, and a quote that does not
    # say which building it is for can be read as being about the other one.
    # Every venue-scoped surface in this app now names its venue for the
    # same reason: the floor API, the AI read API, and the admin band.
    venue: str
    event_date: dt.date
    start_time: dt.time
    end_time: dt.time
    guest_count: int
    spaces: list[SpaceCandidate]
    warnings: list[str]
