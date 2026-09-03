"""Enquiry drafts table and the Phase 2 drafting switches

Revision ID: d4f6b8a1c2e5
Revises: c8e1a3f92d47
Create Date: 2026-09-03

Additive. Both new switches default OFF, so deploying this changes no
behaviour until a human flips drafting_enabled.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4f6b8a1c2e5"
down_revision = "c8e1a3f92d47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_settings",
        sa.Column("drafting_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "ai_settings",
        sa.Column("drafts_visible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_table(
        "enquiry_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=30), nullable=False),
        sa.Column("gate_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("gate_note", sa.Text(), nullable=True),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("rule_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("prompt_version", sa.String(length=20), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("sent_version", sa.Text(), nullable=True),
        sa.Column("edit_reason", sa.Text(), nullable=True),
        sa.Column("discard_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_enquiry_drafts_booking_created", "enquiry_drafts", ["booking_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_enquiry_drafts_booking_created", table_name="enquiry_drafts")
    op.drop_table("enquiry_drafts")
    op.drop_column("ai_settings", "drafts_visible")
    op.drop_column("ai_settings", "drafting_enabled")
