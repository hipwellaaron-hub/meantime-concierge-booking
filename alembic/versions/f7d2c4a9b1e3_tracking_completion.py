"""tracking completion: submission identity, tracking context, dispatch log

- bookings.submission_id: the client-generated id of one form submission,
  unique when present. A retry after a lost response, or a concurrent
  double, resolves to the same booking by this id regardless of the
  15-second duplicate window.
- bookings.tracking_context: what the server saw on the POST that created
  the enquiry -- GA4 client/session ids and Meta browser ids read from the
  parent-domain cookies, plus user agent and client address -- kept only
  so a server-side conversion can be matched to the same browser session.
  Not shown in the UI and never sent in an ordinary analytics event.
- conversion_dispatches: one row per (booking, platform, channel) --
  see app/models/conversion_dispatch.py.

Nullable, default NULL: nothing historical needs backfilling.

Revision ID: f7d2c4a9b1e3
Revises: b4c1e8f27a93
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7d2c4a9b1e3"
down_revision = "b4c1e8f27a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_bookings_submission_id", "bookings", ["submission_id"], unique=True)
    op.add_column("bookings", sa.Column("tracking_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "conversion_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(10), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("event_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("booking_id", "platform", "channel", name="uq_conversion_dispatch_booking_platform_channel"),
    )
    op.create_index("ix_conversion_dispatches_booking_id", "conversion_dispatches", ["booking_id"])
    op.create_index("ix_conversion_dispatches_status_next", "conversion_dispatches", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_conversion_dispatches_status_next", table_name="conversion_dispatches")
    op.drop_index("ix_conversion_dispatches_booking_id", table_name="conversion_dispatches")
    op.drop_table("conversion_dispatches")
    op.drop_column("bookings", "tracking_context")
    op.drop_index("ix_bookings_submission_id", table_name="bookings")
    op.drop_column("bookings", "submission_id")
