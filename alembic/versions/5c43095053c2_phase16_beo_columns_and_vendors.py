"""phase16 BEO overhaul: timeline columns, wizard responses, vendors table

Revision ID: 5c43095053c2
Revises: 37b4408668f8
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5c43095053c2'
down_revision: Union[str, Sequence[str], None] = '37b4408668f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Client-known timeline facts, captured by the wizard's basics step
    # and staff-editable afterward. key_moments is a JSONB list of
    # {"time": "HH:MM"|null, "label": str}. pack_down_notes is staff-only.
    op.add_column('bookings', sa.Column('guest_arrival_time', sa.Time(), nullable=True))
    op.add_column('bookings', sa.Column('key_moments', postgresql.JSONB(), nullable=True))
    op.add_column('bookings', sa.Column('pack_down_notes', sa.Text(), nullable=True))

    # Verbatim client answers for the two new wizard steps -- audit and
    # wizard resumability. booking_vendors below is the authoritative
    # record staff act on.
    op.add_column('wizard_sessions', sa.Column('vendors_response', postgresql.JSONB(), nullable=True))
    op.add_column('wizard_sessions', sa.Column('av_response', postgresql.JSONB(), nullable=True))

    # A real table, not JSONB: staff confirm bump-in per vendor (needs a
    # row identity to POST against), and that confirmation must survive
    # the client re-saving their wizard step. bump_in_confirmed is
    # tri-state exactly like bookings.setup_access_confirmed: NULL = no
    # bump-in requested, FALSE = requested and pending staff confirmation,
    # TRUE = staff-confirmed.
    op.create_table(
        'booking_vendors',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'), nullable=False),
        sa.Column('vendor_type', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('contact_number', sa.String(length=50), nullable=True),
        sa.Column('bump_in_time', sa.Time(), nullable=True),
        sa.Column('bump_in_confirmed', sa.Boolean(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='wizard'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_booking_vendors_booking_id', 'booking_vendors', ['booking_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_booking_vendors_booking_id', table_name='booking_vendors')
    op.drop_table('booking_vendors')
    op.drop_column('wizard_sessions', 'av_response')
    op.drop_column('wizard_sessions', 'vendors_response')
    op.drop_column('bookings', 'pack_down_notes')
    op.drop_column('bookings', 'key_moments')
    op.drop_column('bookings', 'guest_arrival_time')
