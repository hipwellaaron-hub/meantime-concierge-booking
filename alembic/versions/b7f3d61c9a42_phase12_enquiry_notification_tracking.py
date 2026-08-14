"""phase12 enquiry notification tracking

Revision ID: b7f3d61c9a42
Revises: e4b2f97a1c05
Create Date: 2026-08-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b7f3d61c9a42"
down_revision = "e4b2f97a1c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("enquiry_notification_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "enquiry_notification_sent_at")
