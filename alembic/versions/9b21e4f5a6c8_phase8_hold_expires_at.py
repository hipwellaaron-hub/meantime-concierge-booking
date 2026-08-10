"""phase8 hold_expires_at

Revision ID: 9b21e4f5a6c8
Revises: 574a2490fa27
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9b21e4f5a6c8'
down_revision: Union[str, Sequence[str], None] = '574a2490fa27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A hold is a Booking at 'tentative' status (see app/models/booking.py)
    -- this is the one new field it needs: when the hold expires, if ever.
    Only meaningful for tentative bookings; NULL means either "not a hold"
    or "a deliberately open-ended hold", both of which read the same way
    (no expiry to surface)."""
    op.add_column('bookings', sa.Column('hold_expires_at', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'hold_expires_at')
