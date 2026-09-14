"""a receipt keeps the account it was paid to

app.templating.venue_identity reads a venue's bank details LIVE, and
invoice.html prints them on every view and every PDF download. That is
right for an unpaid invoice -- the codebase states the rule: "an unpaid
invoice should always point at the current bank details", because a client
about to pay must be told where the money goes today.

It is wrong for a PAID one. A paid invoice is a receipt: a record of an
account that money actually went to. Today, change a venue's bank details
and every historical paid invoice silently reprints with the new ones, so a
client re-downloading last year's receipt sees an account they never paid.

That has been harmless while one company had one account. Nice Try Events
Pty Ltd is a separate legal entity with its own bank account, and the day
that row is filled in is the day this stops being theoretical -- which is
why Aaron asked for it before the account goes in rather than after.

So the account is snapshotted at the moment the invoice becomes paid.
Nullable and backfilled with nothing: an invoice paid before this migration
has no record of what it was paid to, and inventing one from today's venue
row would be asserting a fact nobody knows. Those keep rendering live, and
the template says which it is showing.

Revision ID: c2f8d61a94b7
Revises: b7e4a91c3f20
Create Date: 2026-09-14 05:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2f8d61a94b7"
down_revision: Union[str, Sequence[str], None] = "b7e4a91c3f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("paid_to_account", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # IF EXISTS, matching b7e4a91c3f20's DROP INDEX IF EXISTS beside it. A
    # downgrade is run in exactly the situations where the schema may not
    # be what the graph says -- a restore, a re-parent, a half-applied
    # chain -- and a downgrade that raises there is a downgrade that cannot
    # be used when it is most needed.
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS paid_to_account")
