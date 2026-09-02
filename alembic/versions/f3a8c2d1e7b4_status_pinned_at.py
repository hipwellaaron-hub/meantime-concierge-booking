"""bookings.status_pinned_at -- manual status override wins over automation

Set when a human changes a booking's status by hand (the staff dropdown).
While set, the automatic status transitions (auto-hold on send, auto-confirm
on deposit+signature) leave the booking alone, so a deliberately confirmed,
deposit-waived booking is never walked anywhere by automation. NULL means
automation may act; cleared by "hand back to automation".

Revision ID: f3a8c2d1e7b4
Revises: e7c2a4f1b9d3
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a8c2d1e7b4"
down_revision = "e7c2a4f1b9d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("status_pinned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "status_pinned_at")
