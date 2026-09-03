"""AI access control: kill-switch settings row and request log

Revision ID: a1c4f7e20b91
Revises: f3a8c2d1e7b4
Create Date: 2026-09-03

Foundation for the AI integration (Phase 1 brief sections 7 and 8). Both
tables are additive and touch nothing existing.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c4f7e20b91"
down_revision = "f3a8c2d1e7b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("access_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("writes_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("writes_disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("writes_disabled_reason", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_ai_settings_singleton"),
    )
    # Seed the singleton so the app never has to create it under load.
    op.execute("INSERT INTO ai_settings (id, access_enabled, writes_enabled) VALUES (1, true, true)")

    op.create_table(
        "ai_request_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id"), nullable=True),
        sa.Column("trigger", sa.String(length=30), nullable=True),
        sa.Column("context", sa.String(length=1000), nullable=True),
    )
    op.create_index("ix_ai_request_log_at", "ai_request_log", ["at"])
    op.create_index("ix_ai_request_log_kind_at", "ai_request_log", ["kind", "at"])


def downgrade() -> None:
    op.drop_index("ix_ai_request_log_kind_at", table_name="ai_request_log")
    op.drop_index("ix_ai_request_log_at", table_name="ai_request_log")
    op.drop_table("ai_request_log")
    op.drop_table("ai_settings")
