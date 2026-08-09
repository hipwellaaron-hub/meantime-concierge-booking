import datetime as dt
import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

# A four-figure venue with a handful of spaces will never legitimately
# need a five-figure guest count; this just blocks obviously-garbage or
# abusive input, not real bookings.
MAX_REASONABLE_ATTENDEE_COUNT = 5000


class EnquiryCreate(BaseModel):
    # Field names match what iVvy currently captures, per the build brief.
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    event_name: str = Field(min_length=1, max_length=255)
    event_date: dt.date
    attendee_count: int = Field(ge=1, le=MAX_REASONABLE_ATTENDEE_COUNT)
    event_type: str | None = Field(default=None, max_length=100)
    proposed_time_slot: str | None = Field(default=None, max_length=100)
    comments: str | None = Field(default=None, max_length=5000)
    space_id: uuid.UUID
    # Optional: populated by the embedding site from its own UTM capture.
    # Falls back to Referer-header classification if omitted.
    lead_source: str | None = None

    @field_validator("name", "event_name")
    @classmethod
    def _not_blank_after_strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("phone", "event_type", "proposed_time_slot", "comments")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class EnquiryResponse(BaseModel):
    reference_code: str
    booking_id: uuid.UUID
    contact_id: uuid.UUID
    possible_duplicate_contact: bool
