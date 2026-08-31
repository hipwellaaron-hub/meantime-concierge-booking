"""phase20 tracking: conversion_emitted_at on bookings

The server-side, authoritative once-only guard for the ad-conversion
snippet: set the first time a booking's thank-you page renders the GA4
function_enquiry_submitted / Meta Lead snippet, so a refresh, Back/Forward,
a duplicate-reuse, or a re-opened confirmation URL never fires a second
conversion. Nullable and defaulting to NULL -- every existing booking is
correctly "never emitted", and nothing historical needs backfilling.

Revision ID: d5b1c7e93a42
Revises: c4f1a9d2e6b8
"""

import sqlalchemy as sa
from alembic import op

revision = "d5b1c7e93a42"
down_revision = "c4f1a9d2e6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("conversion_emitted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "conversion_emitted_at")
