"""Reconciliation findings table

Revision ID: b3d9e6a41c07
Revises: a1c4f7e20b91
Create Date: 2026-09-03

One row per (booking, check). Additive; touches nothing existing.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b3d9e6a41c07"
down_revision = "a1c4f7e20b91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("check_code", sa.String(length=50), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("booking_id", "check_code", name="uq_reconciliation_booking_check"),
    )
    op.create_index("ix_reconciliation_open", "reconciliation_findings", ["resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_reconciliation_open", table_name="reconciliation_findings")
    op.drop_table("reconciliation_findings")
