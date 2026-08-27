import datetime as dt
import enum
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Time, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class VendorType(str, enum.Enum):
    """Validated in Python (Pydantic schema + service checks), stored as a
    plain string -- no DB behaviour branches on the type, so a fourth
    Postgres enum would only add migration friction for zero enforcement
    gain."""

    dj = "dj"
    band = "band"
    decorator = "decorator"
    photographer = "photographer"
    other = "other"


class BookingVendor(Base):
    """An external supplier attached to a booking (DJ, band, decorator,
    photographer). A real table rather than JSONB on the wizard session:
    staff confirm bump-in per vendor, which needs a stable row identity to
    POST against, and that confirmation must survive the client re-saving
    their wizard step. WizardSession.vendors_response keeps the client's
    verbatim answer; these rows are what staff act on.

    bump_in_confirmed is tri-state, exactly the setup_access_confirmed
    pattern on Booking: NULL = no bump-in requested, False = requested by
    the client and pending staff confirmation (a client routinely
    nominates a time their DJ never agreed to -- it must render as a
    request until checked against setup access and the day's other
    bookings), True = staff-confirmed.
    """

    __tablename__ = "booking_vendors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    vendor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bump_in_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    bump_in_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 'wizard' rows are synced from the client's step saves (and may be
    # replaced by a re-save); 'staff' rows were entered on the admin edit
    # screen and are never touched by a wizard re-save.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="wizard")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(back_populates="vendors")

    __table_args__ = (Index("ix_booking_vendors_booking_id", "booking_id"),)
