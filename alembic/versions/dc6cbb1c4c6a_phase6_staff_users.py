"""phase6 staff users

Revision ID: dc6cbb1c4c6a
Revises: 299d2c03d6b4
Create Date: 2026-08-10 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'dc6cbb1c4c6a'
down_revision: Union[str, Sequence[str], None] = '299d2c03d6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'staff_users',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_staff_users_email_lower', 'staff_users', [sa.text('lower(email)')], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_staff_users_email_lower', table_name='staff_users')
    op.drop_table('staff_users')
