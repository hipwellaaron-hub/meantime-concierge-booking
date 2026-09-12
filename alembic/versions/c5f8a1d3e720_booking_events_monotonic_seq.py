"""booking_events gets a monotonic ordering column

Aaron's call, 2026-09-12: do it before step 6 finishes rather than after,
because an intermittent failure here looks exactly like a venue bug during
the one week everything is a venue bug.

THE PROBLEM. `created_at` is `server_default now()`, and Postgres `now()` is
TRANSACTION START time -- so every event written in one transaction shares a
timestamp to the microsecond. Ordering by it is ordering on tied values, and
which row comes back first is then whatever the chosen plan happens to
produce. It has held so far only because booking_events is append-only:
nothing UPDATEs a row, so none moves, and a sequential scan returns heap
order, which is insertion order. That is incidental, not guaranteed -- an
index scan on ix_booking_events_booking_created returns ties in index order
instead, and the plan flips when the table's statistics change.

It is not only a test problem, which is why this is worth a migration:
app/services/document_regeneration.py orders `created_at DESC` to find the
LATEST event of a kind, and a tie there returns an arbitrary one of several.

THE FIX is a column whose values are assigned at INSERT and never tie.
BIGSERIAL: every row gets the next value from a sequence, monotonically, and
two rows in the same transaction differ.

WHY THIS DOES NOT BREAK THE APPEND-ONLY TRIGGER. The trigger refuses UPDATE
unconditionally (only DELETE has the flagged escape hatch c4f1a9d2e6b8 added
for delete_booking_and_dependents), so a backfill UPDATE would be refused.
It does not need one: `ALTER TABLE ... ADD COLUMN ... BIGSERIAL` fills every
existing row as part of the table rewrite, and a row-level trigger does not
fire for that. Verified against the 476 real rows in the dev database, in a
transaction that was rolled back:

  * ADD COLUMN BIGSERIAL succeeded; 476 rows filled, seq 1..476
  * seq follows heap order exactly, so historical rows keep the ordering
    they already had -- the one the old assertions were relying on
  * an ordinary UPDATE afterwards was still refused by the trigger

So history is preserved and the guarantee is intact; the only thing that
changes is that from here the ordering is a fact rather than a coincidence.

Revision ID: c5f8a1d3e720
Revises: b7c2d8e4f316
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c5f8a1d3e720'
down_revision: Union[str, Sequence[str], None] = 'b7c2d8e4f316'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # BIGSERIAL rather than an explicit sequence + default, so the existing
    # rows are filled by the rewrite rather than by an UPDATE the
    # append-only trigger would refuse.
    op.execute("ALTER TABLE booking_events ADD COLUMN seq BIGSERIAL NOT NULL")

    # The query this exists for is "this booking's events, in order", which
    # is exactly the shape of ix_booking_events_booking_created. Same
    # columns, ordered by the column that does not tie.
    op.create_index(
        "ix_booking_events_booking_seq", "booking_events", ["booking_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_booking_events_booking_seq", table_name="booking_events")
    op.execute("ALTER TABLE booking_events DROP COLUMN seq")
