"""One row per drafting attempt on an enquiry (Phase 2 brief).

Every attempt is recorded -- blocked, failed, skipped or generated -- so
shadow mode has a row per enquiry to compare against what a human
actually sent. A gate that only recorded its successes would hide the
calibration signal the brief asks for (section 8: the gate should block
roughly a third; a gate that never blocks is miscalibrated).

The review fields at the bottom are Stage 1's whole purpose: what the AI
would have sent, what a human did send, and why they differed. The
discard reason is the honest measure of whether this is working.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Attempt outcomes. Stable strings, not an enum type, so adding one later
# is a code change rather than a migration.
STATUS_SKIPPED = "skipped"            # drafting off, or no API key
STATUS_BLOCKED = "blocked"            # the triage gate said no; no model call made
STATUS_DATA_MISMATCH = "data_mismatch"  # the two availability paths disagreed; no draft
STATUS_RULES_BLOCKED = "rules_blocked"  # drafted, but a house rule failed; never surfaced
STATUS_GENERATED = "generated"        # drafted and passed every rule
STATUS_FAILED = "failed"              # the model call or something around it broke

# What a human did with it (Stage 1 review).
OUTCOME_SENT_UNCHANGED = "sent_unchanged"
OUTCOME_EDITED = "edited"
OUTCOME_DISCARDED = "discarded"


class EnquiryDraft(Base):
    __tablename__ = "enquiry_drafts"
    __table_args__ = (Index("ix_enquiry_drafts_booking_created", "booking_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False, default="enquiry_received")

    # The gate's verdict, kept whether or not a draft followed.
    gate_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    gate_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Grounding: when the reads were taken. Freshness is re-checked on
    # surface and on approval, never trusted from here.
    as_of: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The draft itself and how it was made. Stored even when rules blocked
    # it, so a blocked draft can be read for calibration -- just never shown
    # as a draft.
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Stage 1 review ---------------------------------------------------
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    discard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    booking: Mapped["Booking"] = relationship()  # noqa: F821
