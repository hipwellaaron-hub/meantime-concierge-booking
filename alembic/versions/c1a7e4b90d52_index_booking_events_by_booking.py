"""booking_events: index by booking, newest first

booking_events had no index at all -- only its primary key. A foreign key
does not create one in Postgres, and this is the append-only audit log
that every write in the app adds to (76 call sites), never deletes from,
and that grows for the life of the venue across every booking.

Every read of it is per-booking and newest-first:

  - Booking.events, loaded on every admin booking page and ordered by
    created_at;
  - documents.was_hand_edited, which filters one booking's events for a
    document version;
  - the reconstruct-the-timeline reads that exist so nobody has to
    re-read an email chain.

All of those scan the whole table today. (a, created_at) also serves a
plain booking_id lookup, so one index covers the lot.

Deliberately not covered: the dashboard's recent-activity query, which
orders by created_at across a whole venue through two joins. That is a
different shape and a different index; it is bounded by LIMIT 60 and is
not the pattern that grows per booking.

Found while reviewing an unrelated branch (2026-09-07). Nothing about the
Event Order work depends on it -- this is worth having on its own.
"""

from alembic import op

revision = "c1a7e4b90d52"
down_revision = "f7d2c4a9b1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_booking_events_booking_created",
        "booking_events",
        ["booking_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_booking_events_booking_created", table_name="booking_events")
