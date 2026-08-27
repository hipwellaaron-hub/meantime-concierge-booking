"""phase15 capacity correction and pricing lock date

Revision ID: 3ad9742be8fc
Revises: 54e0e201860e
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3ad9742be8fc'
down_revision: Union[str, Sequence[str], None] = '54e0e201860e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Real venue capacities changed (per Aaron, 27 Aug 2026) -- app/seed.py's
    # HAMILTON_SPACES only inserts rows that don't already exist by name, so
    # editing that source file alone would never touch the real rows already
    # sitting in this database. A genuine UPDATE is required.
    op.execute("UPDATE spaces SET capacity = 130 WHERE name = 'The Loft'")
    op.execute("UPDATE spaces SET capacity = 70 WHERE name = 'The Mezzanine'")

    # No server_default: the right value depends on each existing booking's
    # own created_at, so it's backfilled per-row below, then the column is
    # flipped to NOT NULL -- same pattern as agreed_min_adults in 1b7abf008413.
    op.add_column('bookings', sa.Column('pricing_locked_at', sa.Date(), nullable=True))
    op.execute("UPDATE bookings SET pricing_locked_at = created_at::date")
    op.alter_column('bookings', 'pricing_locked_at', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('bookings', 'pricing_locked_at')
    op.execute("UPDATE spaces SET capacity = 60 WHERE name = 'The Mezzanine'")
    op.execute("UPDATE spaces SET capacity = 100 WHERE name = 'The Loft'")
