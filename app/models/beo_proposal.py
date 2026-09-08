"""A proposed set of Event Order field values, and what a human did with
each one.

Propose-and-approve, matching the drafting layer: the AI transcribes a
client's final details into the ten free-text Event Order fields and
stores them HERE. Nothing is applied to the document until Aaron approves
it, field by field or all at once. There is deliberately no path that
writes a proposal straight onto an Event Order.

Two tables rather than one JSONB blob, because the per-field record IS
the point: `proposed_value` next to `applied_value` is the measure of
whether this is working. A field Aaron approved untouched is a field the
transcription got right; a field he rewrote first is the calibration
signal, and it is queryable rather than buried in a diff.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Proposal-level state.
STATUS_PENDING = "pending"          # awaiting review; some fields may already be decided
STATUS_RULES_BLOCKED = "rules_blocked"  # a house rule failed; stored for calibration, never surfaced
STATUS_RESOLVED = "resolved"        # every field approved or rejected
STATUS_SUPERSEDED = "superseded"    # a newer proposal replaced the fields still pending here

# Per-field state.
FIELD_PENDING = "pending"
FIELD_APPROVED = "approved"
FIELD_REJECTED = "rejected"
FIELD_SUPERSEDED = "superseded"   # a newer proposal replaced it before anyone saw it
FIELD_BLOCKED = "blocked"         # the house rules refused the proposal it belonged to

PROPOSAL_STATUSES = (STATUS_PENDING, STATUS_RULES_BLOCKED, STATUS_RESOLVED, STATUS_SUPERSEDED)
FIELD_STATES = (FIELD_PENDING, FIELD_APPROVED, FIELD_REJECTED, FIELD_SUPERSEDED, FIELD_BLOCKED)


class BeoProposal(Base):
    __tablename__ = "beo_proposals"
    __table_args__ = (Index("ix_beo_proposals_booking_created", "booking_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The Event Order draft this was proposed against, when one existed at
    # proposal time. Recorded so a proposal can be shown as stale if the
    # document was regenerated underneath it; approval always re-resolves
    # the booking's current draft rather than trusting this.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)

    # Where the values came from, in the proposer's own words -- "client
    # email 6 Sep, final details" -- so an approval can be traced back to
    # something a human can go and read. Required: an untraceable proposal
    # is not reviewable.
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    # Free-text reason the caller acted, mirroring the AI request log.
    trigger: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Set only when the house rules blocked it (see app.services.beo_rules).
    rule_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rule_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-blocking: something true about the finished Event Order that this
    # proposal did not cause and must not be refused for, shown to the
    # reviewer alongside the fields.
    warning_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    warning_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    booking = relationship("Booking", back_populates="beo_proposals")
    fields = relationship(
        "BeoProposalField",
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="BeoProposalField.field",
    )

    @property
    def pending_fields(self) -> list["BeoProposalField"]:
        return [f for f in self.fields if f.state == FIELD_PENDING]

    @property
    def is_reviewable(self) -> bool:
        return self.status == STATUS_PENDING and bool(self.pending_fields)


class BeoProposalField(Base):
    __tablename__ = "beo_proposal_fields"
    __table_args__ = (
        UniqueConstraint("proposal_id", "field", name="uq_beo_proposal_field"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beo_proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=FIELD_PENDING)

    # What the AI proposed, kept verbatim and never overwritten.
    proposed_value: Mapped[str] = mapped_column(Text, nullable=False)
    # What the Event Order held at the moment of approval, so the audit
    # shows what the approval actually changed.
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What was written. Equal to proposed_value when Aaron approved it as
    # offered; different when he edited it first -- which is the signal
    # this whole table exists to carry.
    applied_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    proposal = relationship("BeoProposal", back_populates="fields")

    @property
    def edited_before_approval(self) -> bool:
        return self.state == FIELD_APPROVED and (self.applied_value or "") != (self.proposed_value or "")
