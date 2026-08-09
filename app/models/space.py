import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("venues.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    min_food_spend: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    standard_min_adults: Mapped[int] = mapped_column(Integer, nullable=False)
    wheelchair_accessible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The Lounge is the one space that never charges a per-head shortfall fee
    # below the standard minimum adult count; Loft and Mezzanine do.
    has_per_head_shortfall_fee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # False for internal placeholder spaces (e.g. the migration-triage
    # "Unassigned" space) that must never appear as a real booking option.
    is_bookable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    venue: Mapped["Venue"] = relationship(back_populates="spaces")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="space")

    __table_args__ = (UniqueConstraint("venue_id", "name", name="uq_space_venue_name"),)
