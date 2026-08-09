import datetime as dt
import uuid

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="contact")

    # Deliberately NOT unique: the same person legitimately emails from two
    # addresses. Dedupe is handled by app.services.contact_matching, which
    # surfaces suspected duplicates for a human to confirm rather than
    # merging automatically.
    __table_args__ = (Index("ix_contacts_email_lower", func.lower(email)),)
