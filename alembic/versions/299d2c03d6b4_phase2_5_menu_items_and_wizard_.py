"""phase2.5 menu items and wizard sessions

Revision ID: 299d2c03d6b4
Revises: 1b7abf008413
Create Date: 2026-08-10 08:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '299d2c03d6b4'
down_revision: Union[str, Sequence[str], None] = '1b7abf008413'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE menu_item_category AS ENUM ('platter', 'pizza', 'cake')")
    op.create_table(
        'menu_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('category', postgresql.ENUM(name='menu_item_category', create_type=False), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('current_price', sa.Numeric(10, 2), nullable=False),
        sa.Column('legacy_price', sa.Numeric(10, 2), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category', 'name', name='uq_menu_item_category_name'),
    )

    op.execute("CREATE TYPE wizard_session_status AS ENUM ('pending', 'in_progress', 'submitted', 'revoked')")
    op.execute("CREATE TYPE wizard_step AS ENUM ('basics', 'food', 'beverage', 'music', 'extras', 'review')")
    op.create_table(
        'wizard_sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', postgresql.ENUM(name='wizard_session_status', create_type=False), nullable=False),
        sa.Column('current_step', postgresql.ENUM(name='wizard_step', create_type=False), nullable=False),
        sa.Column('access_token', sa.String(length=64), nullable=False),
        sa.Column('food_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('beverage_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('music_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('extras_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('has_hard_escalation', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('booking_id'),
        sa.UniqueConstraint('access_token'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('wizard_sessions')
    op.execute("DROP TYPE IF EXISTS wizard_step")
    op.execute("DROP TYPE IF EXISTS wizard_session_status")
    op.drop_table('menu_items')
    op.execute("DROP TYPE IF EXISTS menu_item_category")
