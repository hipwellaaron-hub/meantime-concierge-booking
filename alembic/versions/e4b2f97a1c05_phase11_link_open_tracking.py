"""phase11 link open tracking

Revision ID: e4b2f97a1c05
Revises: d92a5c14f6b3
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4b2f97a1c05'
down_revision: Union[str, Sequence[str], None] = 'd92a5c14f6b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A staff member sending a client a link had no way to tell whether
    it was ever actually opened -- these are all set once, the first time
    the corresponding public link is loaded, so "sent but never opened"
    becomes visible rather than indistinguishable from "opened and read"."""
    op.add_column('documents', sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('invoices', sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('wizard_sessions', sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('wizard_sessions', 'opened_at')
    op.drop_column('invoices', 'viewed_at')
    op.drop_column('documents', 'viewed_at')
