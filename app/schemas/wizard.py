import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models.booking_vendor import VendorType
from app.services.wizard import BarStructure, CakeChoiceType, MusicType

MAX_NOTES_LENGTH = 2000

# The beverage step's inclusion checkboxes -- must match
# app.services.wizard_generation.BAR_INCLUSION_LABELS.
BAR_INCLUSION_KEYS = ("beer", "wine", "soft_drinks", "standard_spirits")


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class WizardKeyMoment(BaseModel):
    time: dt.time | None = None
    label: str = Field(min_length=1, max_length=120)

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a key moment needs a label")
        return stripped


class WizardBasicsStep(BaseModel):
    start_time: dt.time
    end_time: dt.time
    food_service_time: dt.time
    setup_access_time: dt.time
    adult_count: int = Field(ge=0, le=5000)
    child_count: int = Field(ge=0, le=5000)
    # Guests frequently arrive later than the host's own access/start time.
    guest_arrival_time: dt.time | None = None
    key_moments: list[WizardKeyMoment] = Field(default_factory=list, max_length=20)


class WizardFoodItem(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(gt=0, le=1000)


class WizardFoodStep(BaseModel):
    # Bounded like every other list on the public wizard payload (key_moments
    # 20, vendors 10, music 3): a real order is a handful of lines per
    # category, and each item triggers a catalogue price lookup server-side,
    # so an unbounded list is needless work to hand a hostile client.
    platters: list[WizardFoodItem] = Field(default_factory=list, max_length=100)
    pizzas: list[WizardFoodItem] = Field(default_factory=list, max_length=100)
    sides: list[WizardFoodItem] = Field(default_factory=list, max_length=100)
    desserts: list[WizardFoodItem] = Field(default_factory=list, max_length=100)


class WizardBeverageStep(BaseModel):
    bar_structure: BarStructure
    bar_limit: Decimal | None = Field(default=None, ge=0)
    # Structured checkboxes (the fix for garbled free-text inclusions like
    # "Everything Bear wine cocktails spirits") -- the str variant is kept
    # so sessions saved before the change still parse.
    bar_inclusions: list[str] | str | None = Field(default=None)
    bar_inclusions_note: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("bar_inclusions")
    @classmethod
    def _validate_bar_inclusions(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            return _strip_optional(value[:MAX_NOTES_LENGTH])
        unknown = [key for key in value if key not in BAR_INCLUSION_KEYS]
        if unknown:
            raise ValueError(f"unknown bar inclusion(s): {', '.join(unknown)}")
        # De-duplicated, in canonical order, so the composed sentence is stable.
        return [key for key in BAR_INCLUSION_KEYS if key in value]

    @field_validator("bar_inclusions_note")
    @classmethod
    def _strip_bar_inclusions_note(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardMusicStep(BaseModel):
    # Multi-select: playlist and DJ/musician are not mutually exclusive --
    # events regularly run a playlist before and after a set. The legacy
    # single music_type is still accepted from older clients.
    music_types: list[MusicType] = Field(default_factory=list, max_length=3)
    music_type: MusicType | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    bump_in_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("music_types")
    @classmethod
    def _dedupe_music_types(cls, value: list[MusicType]) -> list[MusicType]:
        seen = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen

    @field_validator("notes", "bump_in_notes")
    @classmethod
    def _strip_notes(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardReviewStep(BaseModel):
    # "Anything else we should know?" -- the review step's escape hatch.
    final_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("final_notes")
    @classmethod
    def _strip_final_notes(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardExtrasStep(BaseModel):
    cake_choice_type: CakeChoiceType
    cake_menu_item_id: uuid.UUID | None = None
    cake_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    decorations_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    layout_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    dietary_requirements: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    # Deliberately free text, not a checkbox -- but ANY non-blank value
    # against a non-wheelchair-accessible space (Loft/Mezzanine) triggers a
    # hard escalation to Aaron. See app.services.wizard.flag_accessibility_escalation.
    accessibility_needs: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    additional_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator(
        "cake_notes",
        "decorations_notes",
        "layout_notes",
        "dietary_requirements",
        "accessibility_needs",
        "additional_notes",
    )
    @classmethod
    def _strip_all_notes(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardVendor(BaseModel):
    vendor_type: VendorType
    name: str = Field(min_length=1, max_length=255)
    contact_number: str | None = Field(default=None, max_length=50)
    # A REQUEST, never a confirmation -- see app.models.booking_vendor.
    bump_in_time: dt.time | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a vendor needs a name")
        return stripped

    @field_validator("contact_number")
    @classmethod
    def _strip_contact(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardVendorsStep(BaseModel):
    # An empty list is a real "no vendors" answer, distinct from the step
    # never having been saved.
    vendors: list[WizardVendor] = Field(default_factory=list, max_length=10)


class WizardAvStep(BaseModel):
    video_slideshow: bool
    microphones_for_speeches: bool
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, value: str | None) -> str | None:
        return _strip_optional(value)
