"""AI integration access control and request log (Phase 1 brief §7, §8).

Two tables, both deliberately kept OUT of BookingEvent:

- AiSettings -- a single row holding the kill switches. A *database* flag,
  not an environment variable, because changing an env var on Railway
  triggers a rebuild of roughly ninety seconds. That is not a kill switch.
  The env vars remain as a backstop: either source saying "off" means off,
  so the switch can never be turned back on by editing only one of them.

- AiRequestLog -- every AI request, read or write. Separate from
  BookingEvent so a few hundred reads a day cannot bury the writes in the
  audit trail staff actually read (it feeds the dashboard's recent
  activity). It doubles as the source of truth for write rate limiting,
  which is why the limiter survives a restart: an auto-disable that reset
  itself on the next deploy would be worse than no limiter at all.
"""

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Boolean,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The single actor string every AI action is attributed to. One constant so
# a query for "what has the AI done" can never miss a spelling.
AI_ACTOR = "ai:claude"


class AiRequestKind(str, enum.Enum):
    read = "read"
    write = "write"


class AiTrigger(str, enum.Enum):
    """Why the AI acted (brief §8). Self-reported by the caller, so this is
    a breadcrumb for reading the log later, never a control -- the actual
    control is which endpoints exist at all."""

    staff_request = "staff_request"
    reconciliation = "reconciliation"
    proactive = "proactive"



class AiSettings(Base):
    """Exactly one row (id = 1, enforced by a check constraint). Read on
    every AI request; written only by staff toggling the switch or by the
    limiter auto-disabling writes."""

    __tablename__ = "ai_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ai_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)

    access_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    writes_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)

    # Set when the rate limiter trips, so the reason survives a restart and
    # a staff member can see why writes went off without reading logs.
    writes_disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    writes_disabled_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AiRequestLog(Base):
    """One row per AI request. Params are stored as JSON rather than a
    query string so a later audit can filter on them; nothing here holds
    document content, card details or bank details (brief §3.6)."""

    __tablename__ = "ai_request_log"
    __table_args__ = (
        # The write-rate query ("how many writes since T") reads exactly
        # this pair, and it is on the hot path of every write.
        Index("ix_ai_request_log_kind_at", "kind", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # AiRequestKind value
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    actor: Mapped[str] = mapped_column(String(255), nullable=False, default=AI_ACTOR)

    # Only writes carry these; a read is just a read.
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True
    )
    trigger: Mapped[str | None] = mapped_column(String(30), nullable=True)
    context: Mapped[str | None] = mapped_column(String(1000), nullable=True)
