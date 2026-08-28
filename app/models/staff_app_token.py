import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StaffAppToken(Base):
    """A bearer token for the Meantime Floor app (the read-only staff PWA).

    Only the sha256 hash of the token is ever stored -- the raw value is
    shown once at login and never again, so a leaked database row cannot
    be replayed. Revocation is a timestamp, not a delete: the admin staff
    page lists live tokens per person and revokes them individually, and
    a revoked row keeps its last_used_at as a record of when that device
    was last seen.
    """

    __tablename__ = "staff_app_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("staff_users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    staff_user: Mapped["StaffUser"] = relationship()

    __table_args__ = (Index("ix_staff_app_tokens_staff_user_id", "staff_user_id"),)
