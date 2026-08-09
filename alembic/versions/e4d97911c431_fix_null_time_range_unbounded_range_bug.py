"""fix null time_range unbounded-range bug

Revision ID: e4d97911c431
Revises: 0659300da7ab
Create Date: 2026-08-05 22:27:09.977845

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4d97911c431'
down_revision: Union[str, Sequence[str], None] = '0659300da7ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    tsrange(NULL, NULL, '[)') returns the *unbounded* range, not SQL NULL
    -- so a booking with unknown start/end time was colliding with every
    other unknown-time booking in the same space via the exclusion
    constraint (found by the Phase 5 importer's own tests). Postgres
    won't let a generated column's expression be altered in place, so the
    constraint and column are dropped and recreated with an explicit
    CASE that returns a real NULL for unknown times.
    """
    op.execute("ALTER TABLE bookings DROP CONSTRAINT excl_booking_space_time_overlap")
    op.execute("ALTER TABLE bookings DROP COLUMN time_range")
    op.execute(
        """
        ALTER TABLE bookings ADD COLUMN time_range tsrange GENERATED ALWAYS AS (
            CASE WHEN start_time IS NULL OR end_time IS NULL THEN NULL
            ELSE tsrange(event_date + start_time, event_date + end_time, '[)') END
        ) STORED
        """
    )
    op.execute(
        """
        ALTER TABLE bookings ADD CONSTRAINT excl_booking_space_time_overlap
        EXCLUDE USING gist (space_id WITH =, time_range WITH &&)
        WHERE (status IN ('tentative', 'confirmed', 'completed'))
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE bookings DROP CONSTRAINT excl_booking_space_time_overlap")
    op.execute("ALTER TABLE bookings DROP COLUMN time_range")
    op.execute(
        """
        ALTER TABLE bookings ADD COLUMN time_range tsrange GENERATED ALWAYS AS (
            tsrange(event_date + start_time, event_date + end_time, '[)')
        ) STORED
        """
    )
    op.execute(
        """
        ALTER TABLE bookings ADD CONSTRAINT excl_booking_space_time_overlap
        EXCLUDE USING gist (space_id WITH =, time_range WITH &&)
        WHERE (status IN ('tentative', 'confirmed', 'completed'))
        """
    )
