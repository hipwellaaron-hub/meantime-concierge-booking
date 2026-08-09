import datetime as dt
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookingEventType(str, enum.Enum):
    created = "created"
    status_changed = "status_changed"
    field_changed = "field_changed"


class BookingEvent(Base):
    """Append-only audit log. Rows are never updated or deleted (enforced by
    a DB trigger, see the migration) — reconstructing how a booking got to
    its current state must never require re-reading email chains."""

    __tablename__ = "booking_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking: Mapped["Booking"] = relationship(back_populates="events")
