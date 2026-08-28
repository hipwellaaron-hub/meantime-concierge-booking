"""phase18 Meantime Floor: staff roles and app tokens

Revision ID: a5579e36069c
Revises: 2d4e897f8ad1
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a5579e36069c'
down_revision: Union[str, Sequence[str], None] = '2d4e897f8ad1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 'admin' (full Concierge access) vs 'floor' (the read-only staff app
    # only -- require_staff rejects them from /admin outright). Existing
    # accounts are all admins; the server default backfills them.
    op.add_column(
        'staff_users', sa.Column('role', sa.String(length=20), nullable=False, server_default='admin')
    )

    # Bearer tokens for the Meantime Floor app. Only a sha256 hash is
    # stored -- a leaked database row can't be replayed as a login.
    # Individually revocable from the admin staff page.
    op.create_table(
        'staff_app_tokens',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('staff_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('staff_users.id'), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_staff_app_tokens_staff_user_id', 'staff_app_tokens', ['staff_user_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_staff_app_tokens_staff_user_id', table_name='staff_app_tokens')
    op.drop_table('staff_app_tokens')
    op.drop_column('staff_users', 'role')
