import datetime as dt
import enum
import secrets
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WizardSessionStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    submitted = "submitted"
    revoked = "revoked"


class WizardStep(str, enum.Enum):
    basics = "basics"
    food = "food"
    beverage = "beverage"
    music = "music"
    extras = "extras"
    review = "review"


wizard_session_status_enum = SAEnum(WizardSessionStatus, name="wizard_session_status", native_enum=True)
wizard_step_enum = SAEnum(WizardStep, name="wizard_step", native_enum=True)


def generate_access_token() -> str:
    return secrets.token_urlsafe(32)


class WizardSession(Base):
    """One row per booking (1:1) -- a booking has a single event date, so
    a single wizard, unlike Document which needs per-type versioning.

    Resumability: each non-Basics step is one nullable JSONB column,
    overwritten whole on every save of that step (not merged field by
    field). NULL means "not yet saved". Basics has no JSONB column here --
    it writes straight to Booking's own columns (see app/models/booking.py),
    since those are durable event facts the rest of the system already
    models, not wizard-only state.
    """

    __tablename__ = "wizard_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False, unique=True
    )
    status: Mapped[WizardSessionStatus] = mapped_column(
        wizard_session_status_enum, nullable=False, default=WizardSessionStatus.pending
    )
    current_step: Mapped[WizardStep] = mapped_column(wizard_step_enum, nullable=False, default=WizardStep.basics)
    access_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, default=generate_access_token)

    food_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    beverage_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    music_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extras_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Materialized flag for a future staff dashboard view -- the actual
    # audit record of *why* is a distinct BookingEvent (event_type
    # "accessibility_escalation"), this is not a second source of truth,
    # just a fast, indexed pointer to it.
    has_hard_escalation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # "Expire after the event" (Master Policy doesn't give an exact
    # duration) -- see app.services.policy.WIZARD_TOKEN_TTL_DAYS.
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, the first time the link is opened. `status` alone can't
    # tell "never opened" apart from "opened, but closed before saving
    # anything" -- both sit at pending/basics until a step is actually
    # saved (see app.services.wizard's editable-guard).
    opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    booking: Mapped["Booking"] = relationship(back_populates="wizard_session")
