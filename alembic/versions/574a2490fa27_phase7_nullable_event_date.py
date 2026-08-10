"""phase7 nullable event_date

Revision ID: 574a2490fa27
Revises: dc6cbb1c4c6a
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '574a2490fa27'
down_revision: Union[str, Sequence[str], None] = 'dc6cbb1c4c6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    A real enquiry can arrive with no date locked in yet. time_range is a
    GENERATED column referencing event_date, and the exclusion constraint
    indexes time_range -- Postgres won't let a generated column's
    expression be altered in place (same situation as e4d97911c431), so
    both are dropped and recreated with event_date IS NULL added to the
    guard, around the NOT NULL drop on event_date itself.
    """
    op.execute("ALTER TABLE bookings DROP CONSTRAINT excl_booking_space_time_overlap")
    op.execute("ALTER TABLE bookings DROP COLUMN time_range")

    op.alter_column('bookings', 'event_date', existing_type=sa.Date(), nullable=True)

    op.execute(
        """
        ALTER TABLE bookings ADD COLUMN time_range tsrange GENERATED ALWAYS AS (
            CASE WHEN event_date IS NULL OR start_time IS NULL OR end_time IS NULL THEN NULL
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

    op.alter_column('bookings', 'event_date', existing_type=sa.Date(), nullable=False)

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
