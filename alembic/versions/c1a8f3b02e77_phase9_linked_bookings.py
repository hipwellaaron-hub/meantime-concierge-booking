"""phase9 linked bookings

Revision ID: c1a8f3b02e77
Revises: 9b21e4f5a6c8
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c1a8f3b02e77'
down_revision: Union[str, Sequence[str], None] = '9b21e4f5a6c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A booking that genuinely needs two physical spaces for one event
    (real historical cases surfaced during the iVvy reconciliation) was
    previously unrepresentable -- Booking.space_id is a single FK. Rather
    than inventing a pseudo "Loft + Mezzanine" Space row (which would lie
    about capacity, standard_min_adults, and every other per-room fact),
    a second real Booking row is linked to the first via parent_booking_id.
    The parent carries the contact, documents, invoices, and wizard
    session; a linked child is purely a second space-and-time row that
    rides behind the same GIST exclusion constraint as any other booking.
    Self-referential, nullable -- most bookings are not part of a link."""
    op.add_column('bookings', sa.Column('parent_booking_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_bookings_parent_booking_id', 'bookings', 'bookings', ['parent_booking_id'], ['id']
    )
    op.create_index('ix_bookings_parent_booking_id', 'bookings', ['parent_booking_id'])


def downgrade() -> None:
    op.drop_index('ix_bookings_parent_booking_id', table_name='bookings')
    op.drop_constraint('fk_bookings_parent_booking_id', 'bookings', type_='foreignkey')
    op.drop_column('bookings', 'parent_booking_id')
