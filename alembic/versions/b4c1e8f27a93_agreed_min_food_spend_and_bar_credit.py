"""Per-booking food minimum and bar credit

A signed agreement went out carrying $1,000 in its header summary and
$500 in its Minimum Spend clause (HAM-20261024-M1QZQ, 2026-09-05): the
header rendered the space default while the clause had been hand-edited
to the figure actually agreed. There was nowhere to record that figure,
so there was nothing for the header to read.

agreed_min_food_spend mirrors agreed_min_adults exactly -- NOT NULL,
seeded from the space at creation -- so that no consumer can reach for
Space.min_food_spend and get a different answer to the one on the
contract. Existing rows are backfilled from their own space, which is
precisely what every existing agreement already says, so no document's
meaning changes.

bar_credit replaces typing "$250 bar credit" into the clause as free
text, which kept it off the Event Order and away from the floor.

Revision ID: b4c1e8f27a93
Revises: e2a7c95f1d34
"""

import sqlalchemy as sa
from alembic import op

revision = "b4c1e8f27a93"
down_revision = "e2a7c95f1d34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Most food-spend reductions are a Friday inducement, which none of the
    # existing codes described. IF NOT EXISTS so a re-run is safe; the value
    # is not used by this migration, only by later writes, so adding it
    # inside the migration transaction is fine on PG 12+.
    op.execute("ALTER TYPE min_reduction_reason ADD VALUE IF NOT EXISTS 'friday_incentive'")

    # Added nullable, backfilled, then made NOT NULL: the table has live
    # rows, so a direct NOT NULL add would fail.
    op.add_column("bookings", sa.Column("agreed_min_food_spend", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "bookings",
        sa.Column(
            "agreed_min_food_spend_reason",
            sa.Enum(name="min_reduction_reason", native_enum=True, create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("bar_credit", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
    )

    # Every existing booking keeps exactly the figure its agreement already
    # states: its own space's standard. No reason code is written, because
    # none of these were reductions -- they are the standard.
    op.execute(
        """
        UPDATE bookings b
           SET agreed_min_food_spend = s.min_food_spend
          FROM spaces s
         WHERE s.id = b.space_id
           AND b.agreed_min_food_spend IS NULL
        """
    )
    # Belt and braces for any row whose space somehow did not join.
    op.execute("UPDATE bookings SET agreed_min_food_spend = 0 WHERE agreed_min_food_spend IS NULL")

    op.alter_column("bookings", "agreed_min_food_spend", nullable=False)


def downgrade() -> None:
    op.drop_column("bookings", "bar_credit")
    op.drop_column("bookings", "agreed_min_food_spend_reason")
    op.drop_column("bookings", "agreed_min_food_spend")
    # The enum value is deliberately left in place: PostgreSQL cannot drop
    # one, and rebuilding the type would require rewriting every column
    # that uses it. An unused label is harmless.
