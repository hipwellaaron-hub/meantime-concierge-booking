import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StaffUser(Base):
    """No public registration route exists anywhere in this app -- accounts
    are provisioned by a human running app.create_staff_user once, never by
    an HTTP request. See app/admin_auth.py for how a session is established
    from a set of valid credentials."""

    __tablename__ = "staff_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # WHICH VENUE THIS PERSON WORKS AT, and NULL MEANS SOMETHING HERE.
    #
    # Everywhere else in this codebase a NULL venue means "nobody has said"
    # and nothing is inferred from it. This column is the exception: NULL
    # means EVERY venue, which is what an admin is. Aaron is the only admin
    # and works both buildings, so his account carries no venue and he picks
    # one when signing a phone into the floor app.
    #
    # A FLOOR account always names its venue. Floor staff never see the
    # picker and cannot choose (Aaron, 2026-09-12): a casual who works one
    # building should not be able to put the other building's run sheets on
    # their phone by choosing from a list.
    venue_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venues.id"), nullable=True
    )
    venue: Mapped["Venue | None"] = relationship()
    # 'admin' = full Concierge access; 'floor' = the read-only Meantime
    # Floor app ONLY -- app/admin_auth.py rejects floor sessions from
    # /admin outright. Plain string validated in Python, matching how
    # BookingVendor.vendor_type avoids a native enum's migration friction.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Unlike Contact.email (deliberately non-unique, same person can have
    # two addresses), a staff login must be unambiguous -- unique, not just
    # indexed.
    __table_args__ = (Index("uq_staff_users_email_lower", func.lower(email), unique=True),)
