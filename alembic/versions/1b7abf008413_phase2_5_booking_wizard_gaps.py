"""phase2.5 booking wizard schema gaps

Revision ID: 1b7abf008413
Revises: e4d97911c431
Create Date: 2026-08-10 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1b7abf008413'
down_revision: Union[str, Sequence[str], None] = 'e4d97911c431'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE TYPE min_reduction_reason AS ENUM "
        "('friday_fill', 'weekend_gap', 'returning_client', 'spend_clears_anyway', 'aaron_discretion')"
    )

    # No server_default: the right value depends on which space each
    # existing booking is in, so it's backfilled per-row below, then the
    # column is flipped to NOT NULL.
    op.add_column('bookings', sa.Column('agreed_min_adults', sa.Integer(), nullable=True))
    op.add_column(
        'bookings',
        sa.Column(
            'agreed_min_reduction_reason',
            postgresql.ENUM(name='min_reduction_reason', create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        'bookings', sa.Column('outside_cake_permitted', sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column('bookings', sa.Column('food_service_time', sa.Time(), nullable=True))
    op.add_column('bookings', sa.Column('setup_access_time', sa.Time(), nullable=True))
    op.add_column('bookings', sa.Column('setup_access_confirmed', sa.Boolean(), nullable=True))

    # Backfill: every existing booking inherits its own space's standard
    # minimum, matching "agreed minimum defaults to the standard."
    op.execute(
        """
        UPDATE bookings SET agreed_min_adults = spaces.standard_min_adults
        FROM spaces WHERE bookings.space_id = spaces.id
        """
    )
    op.alter_column('bookings', 'agreed_min_adults', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('bookings', 'setup_access_confirmed')
    op.drop_column('bookings', 'setup_access_time')
    op.drop_column('bookings', 'food_service_time')
    op.drop_column('bookings', 'outside_cake_permitted')
    op.drop_column('bookings', 'agreed_min_reduction_reason')
    op.drop_column('bookings', 'agreed_min_adults')
    op.execute("DROP TYPE IF EXISTS min_reduction_reason")
