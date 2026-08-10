import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.services.wizard import BarStructure, CakeChoiceType, MusicType

MAX_NOTES_LENGTH = 2000


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class WizardBasicsStep(BaseModel):
    start_time: dt.time
    end_time: dt.time
    food_service_time: dt.time
    setup_access_time: dt.time
    adult_count: int = Field(ge=0, le=5000)
    child_count: int = Field(ge=0, le=5000)


class WizardFoodItem(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(gt=0, le=1000)


class WizardFoodStep(BaseModel):
    platters: list[WizardFoodItem] = Field(default_factory=list)
    pizzas: list[WizardFoodItem] = Field(default_factory=list)


class WizardBeverageStep(BaseModel):
    bar_structure: BarStructure
    bar_limit: Decimal | None = Field(default=None, ge=0)
    bar_inclusions: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("bar_inclusions")
    @classmethod
    def _strip_bar_inclusions(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class WizardMusicStep(BaseModel):
    music_type: MusicType
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    bump_in_notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("notes", "bump_in_notes")
    @classmethod
    def _strip_notes(cls, value: str | None) -> str | None:
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
