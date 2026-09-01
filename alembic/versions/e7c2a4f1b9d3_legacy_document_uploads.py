"""legacy document/invoice uploads (iVvy migration)

A legacy document is an inert record of what was signed/paid in iVvy: the
original PDF stored as opaque bytes, plus the metadata needed to (a) tell it
apart from a live generated document and (b) warn if the booking it's
attached to later diverges from what the PDF actually says.

- is_legacy: marks the row a fixed record -- never regenerated, edited, sent,
  revised, or rendered to a client.
- legacy_file / legacy_filename: the uploaded PDF bytes and its name. NULL
  until a PDF is attached (the migration import creates the row first; the
  signed PDF is uploaded against it afterwards).
- legacy_source_ref: the iVvy booking code.
- legacy_snapshot: the booking facts the PDF represents (event_date, space)
  captured at creation, compared to the live booking to surface a mismatch.

Revision ID: e7c2a4f1b9d3
Revises: d5b1c7e93a42
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e7c2a4f1b9d3"
down_revision = "d5b1c7e93a42"
branch_labels = None
depends_on = None

_TABLES = ("documents", "invoices")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.add_column(table, sa.Column("legacy_file", sa.LargeBinary(), nullable=True))
        op.add_column(table, sa.Column("legacy_filename", sa.String(length=255), nullable=True))
        op.add_column(table, sa.Column("legacy_source_ref", sa.String(length=100), nullable=True))
        op.add_column(table, sa.Column("legacy_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "legacy_snapshot")
        op.drop_column(table, "legacy_source_ref")
        op.drop_column(table, "legacy_filename")
        op.drop_column(table, "legacy_file")
        op.drop_column(table, "is_legacy")
