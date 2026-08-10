import datetime as dt
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# A four-figure venue with a handful of spaces will never legitimately
# need a five-figure guest count; this just blocks obviously-garbage or
# abusive input, not real bookings.
MAX_REASONABLE_ATTENDEE_COUNT = 5000

# The event-type dropdown, as actually used on the current iVvy enquiry
# form -- kept as a fixed list (not a DB enum) so it lives in exactly one
# place and matches what staff already recognise.
EVENT_TYPES: tuple[str, ...] = (
    "18th Birthday",
    "21st Birthday",
    "Birthday",
    "Engagement Party",
    "Wedding",
    "Christmas Party",
    "Corporate Function",
    "Baby Shower",
    "Hens or Bucks",
    "Wake or Memorial",
    "Group Lunch or Dinner",
    "Not sure yet",
)

EventType = Literal[
    "18th Birthday",
    "21st Birthday",
    "Birthday",
    "Engagement Party",
    "Wedding",
    "Christmas Party",
    "Corporate Function",
    "Baby Shower",
    "Hens or Bucks",
    "Wake or Memorial",
    "Group Lunch or Dinner",
    "Not sure yet",
]


class EnquiryCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=255)
    last_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    company_name: str | None = Field(default=None, max_length=255)

    event_name: str = Field(min_length=1, max_length=255)
    event_date: dt.date
    dates_flexible: bool
    event_type: EventType
    attendee_count: int = Field(ge=1, le=MAX_REASONABLE_ATTENDEE_COUNT)
    # Not asked up front on the current form -- only relevant once staff
    # need to confirm a shortfall/minimum-spend figure (which counts
    # adults only). Left blank here, it gets flagged for follow-up on
    # birthday-category enquiries -- see app.services.enquiry_classification.
    adult_count: int | None = Field(default=None, ge=0, le=MAX_REASONABLE_ATTENDEE_COUNT)
    proposed_time_slot: str | None = Field(default=None, max_length=100)
    comments: str | None = Field(default=None, max_length=5000)

    # Optional: populated by the embedding site from its own UTM capture.
    # Falls back to Referer-header classification if omitted.
    lead_source: str | None = None

    @field_validator("first_name", "last_name", "event_name")
    @classmethod
    def _not_blank_after_strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("phone", "company_name", "proposed_time_slot", "comments")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("adult_count", mode="before")
    @classmethod
    def _blank_adult_count_is_none(cls, value):
        # An HTML <input type=number>, left empty, submits "" (form
        # encoding always sends the field, unlike JSON where it could
        # simply be omitted) -- int("") raises, so this has to be treated
        # as "not provided" before Pydantic's own int coercion runs.
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def _adult_count_not_more_than_attendees(self) -> "EnquiryCreate":
        if self.adult_count is not None and self.adult_count > self.attendee_count:
            raise ValueError("adult_count cannot exceed attendee_count")
        return self
