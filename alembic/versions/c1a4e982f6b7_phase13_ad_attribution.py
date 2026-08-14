"""phase13 ad attribution

Revision ID: c1a4e982f6b7
Revises: b7f3d61c9a42
Create Date: 2026-08-14 02:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c1a4e982f6b7"
down_revision = "b7f3d61c9a42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("first_touch_attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("bookings", sa.Column("last_touch_attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "last_touch_attribution")
    op.drop_column("bookings", "first_touch_attribution")
