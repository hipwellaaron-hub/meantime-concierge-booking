"""Findings from the nightly reconciliation job (brief section 9).

A dedicated table rather than a BookingEvent, for one reason: the brief
requires findings to be deduplicated and to "clear automatically when the
condition resolves". BookingEvent is append-only by design -- it is the
audit trail, and an audit entry that could be retracted would be worth
less than one that cannot. A finding, by contrast, is a current-state
observation: it opens, it persists across nightly runs without being
re-raised, and it closes when the underlying problem goes away.

One row per (booking, check). A finding that resolves and later recurs
reopens the same row, so the history of a recurring problem stays in one
place instead of scattering across dozens of rows.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ReconciliationFinding(Base):
    __tablename__ = "reconciliation_findings"
    __table_args__ = (
        # Dedup key: the same problem on the same booking is one finding,
        # however many nights it survives.
        UniqueConstraint("booking_id", "check_code", name="uq_reconciliation_booking_check"),
        Index("ix_reconciliation_open", "resolved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )

    check_code: Mapped[str] = mapped_column(String(50), nullable=False)
    # One of the brief's section 4.3 categories, so a finding raised by the
    # job and a flag raised by the AI are the same kind of thing to staff.
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(String(1000), nullable=False)

    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # NULL means open. Set by a run that no longer observes the condition.
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    booking: Mapped["Booking"] = relationship()  # noqa: F821
