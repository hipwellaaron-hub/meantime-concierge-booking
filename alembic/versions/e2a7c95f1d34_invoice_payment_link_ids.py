"""Track every Stripe Payment Link created for an invoice

Revision ID: e2a7c95f1d34
Revises: d4f6b8a1c2e5
Create Date: 2026-09-04

A Payment Link has no expiry of its own and a fresh one is created on
every invoice-page view, so nothing today can reach back and deactivate
one a client already has. This column is what cancel_invoice reads to
call Stripe and kill every link it ever handed out for the invoice.
Additive; existing rows backfill to an empty list.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2a7c95f1d34"
down_revision = "d4f6b8a1c2e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "stripe_payment_link_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("invoices", "stripe_payment_link_ids")
