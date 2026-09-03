"""Separate the client's own enquiry text from staff notes

Revision ID: c8e1a3f92d47
Revises: b3d9e6a41c07
Create Date: 2026-09-03

Additive: one nullable column. Existing bookings.notes is left exactly as
it is -- what changes is that it no longer defaults into the client-facing
Special Notes of a generated Event Order (see
app/services/document_generation.py), so nothing already stored can
publish itself.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8e1a3f92d47"
down_revision = "b3d9e6a41c07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("enquiry_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "enquiry_text")
