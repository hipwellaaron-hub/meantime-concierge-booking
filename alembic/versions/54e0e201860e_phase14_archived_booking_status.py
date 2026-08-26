"""phase14 archived booking status

Revision ID: 54e0e201860e
Revises: c1a4e982f6b7
Create Date: 2026-08-26 09:30:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "54e0e201860e"
down_revision = "c1a4e982f6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres requires ADD VALUE to run outside an explicit transaction
    # block when the new value might be used later in the same session --
    # autocommit_block() takes care of that. Nothing in this migration
    # itself uses 'archived' (see app.archive_bookings_before, a separate
    # one-off script run after this is deployed), so this is the only
    # statement here.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE booking_status ADD VALUE IF NOT EXISTS 'archived'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE. Safely reversing this
    # would mean creating a new enum type without 'archived', rewriting
    # every row currently using it to some other status first (a real
    # data decision, not something a migration should guess at), then
    # swapping the column over -- not something to automate blindly.
    raise NotImplementedError(
        "Cannot automatically downgrade: Postgres does not support removing a value from an "
        "existing enum type. If 'archived' is genuinely no longer needed, first migrate every "
        "booking off that status by hand, then write a real recreate-the-type migration."
    )
