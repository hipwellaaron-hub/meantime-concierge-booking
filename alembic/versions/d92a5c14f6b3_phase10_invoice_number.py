"""phase10 invoice number

Revision ID: d92a5c14f6b3
Revises: c1a8f3b02e77
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd92a5c14f6b3'
down_revision: Union[str, Sequence[str], None] = 'c1a8f3b02e77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A short, human-readable invoice reference for a client to quote back
    -- the UUID id/access_token were never fit for that. Starts at 1001:
    Concierge's own fresh numbering, not a continuation of the prior iVvy
    sequence (there's no reliable source for exactly where that left off)."""
    op.execute("CREATE SEQUENCE invoice_number_seq START WITH 1001")
    op.add_column(
        'invoices',
        sa.Column('invoice_number', sa.Integer(), server_default=sa.text("nextval('invoice_number_seq')"), nullable=False),
    )
    op.create_unique_constraint('uq_invoices_invoice_number', 'invoices', ['invoice_number'])
    op.execute("ALTER SEQUENCE invoice_number_seq OWNED BY invoices.invoice_number")


def downgrade() -> None:
    op.drop_constraint('uq_invoices_invoice_number', 'invoices', type_='unique')
    op.drop_column('invoices', 'invoice_number')
    op.execute("DROP SEQUENCE IF EXISTS invoice_number_seq")
