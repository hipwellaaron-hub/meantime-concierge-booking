import datetime as dt

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# A four-figure venue with a handful of spaces will never legitimately
# need a five-figure guest count; this just blocks obviously-garbage or
# abusive input, not real bookings.
MAX_REASONABLE_ATTENDEE_COUNT = 5000

EVENT_TYPE_MAX_LENGTH = 100  # matches Booking.event_type's column width

# The event-type dropdown on the public form itself -- what the <select>
# actually offers a new enquirer. Deliberately NOT enforced as a strict
# schema-level allow-list (see event_type below): replayed/historical
# enquiries (iVvy's own dropdown had different, looser categories --
# "Christmas", "Party", "Event") and any future source need to still be
# accepted and classified, not hard-rejected for using a category this
# form's own dropdown doesn't happen to offer.
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


class EnquiryCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=255)
    last_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    company_name: str | None = Field(default=None, max_length=255)

    event_name: str = Field(min_length=1, max_length=255)
    # Both left optional -- a real client genuinely may not have a date
    # locked in yet ("not sure of dates, are you flexible?") or may only
    # give a guest-count range in free text. Rejecting the submission
    # outright would lose the lead entirely; instead these get accepted
    # and flagged for staff follow-up -- see app.services.enquiry_classification.
    event_date: dt.date | None = Field(default=None)
    dates_flexible: bool
    event_type: str = Field(min_length=1, max_length=EVENT_TYPE_MAX_LENGTH)
    attendee_count: int | None = Field(default=None, ge=1, le=MAX_REASONABLE_ATTENDEE_COUNT)
    # Not asked up front on the current form -- only relevant once staff
    # need to confirm a shortfall/minimum-spend figure (which counts
    # adults only). Left blank here, it gets flagged for follow-up on
    # enquiries that do give a guest count -- see app.services.enquiry_classification.
    adult_count: int | None = Field(default=None, ge=0, le=MAX_REASONABLE_ATTENDEE_COUNT)
    proposed_time_slot: str | None = Field(default=None, max_length=100)
    comments: str | None = Field(default=None, max_length=5000)

    # Optional: populated by the embedding site from its own UTM capture.
    # Falls back to Referer-header classification if omitted.
    lead_source: str | None = None

    # Raw JSON built client-side from what app/templates/enquiry.html
    # captured at first landing and persisted in localStorage (see
    # app.services.attribution) -- {"first_touch": {...}, "last_touch":
    # {...}}. Never trusted or validated beyond a length cap here; parsing
    # and validation happen in app.services.attribution.parse_attribution_payload,
    # which never raises -- malformed attribution data must never block a
    # real enquiry from being captured.
    attribution: str | None = Field(default=None, max_length=4000)

    @field_validator("first_name", "last_name", "event_name")
    @classmethod
    def _not_blank_after_strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("event_type")
    @classmethod
    def _event_type_not_blank_after_strip(cls, value: str) -> str:
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

    @field_validator("adult_count", "attendee_count", "event_date", mode="before")
    @classmethod
    def _blank_string_is_none(cls, value):
        # A real HTML form always submits every field, even ones left
        # empty (an <input type=number> or type=date left blank submits
        # "", never an omitted key) -- int("")/date.fromisoformat("") both
        # raise, so a blank string has to be treated as "not provided"
        # before Pydantic's own type coercion runs, for every field that's
        # genuinely optional.
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def _adult_count_not_more_than_attendees(self) -> "EnquiryCreate":
        if self.adult_count is not None and self.attendee_count is not None and self.adult_count > self.attendee_count:
            raise ValueError("adult_count cannot exceed attendee_count")
        return self
