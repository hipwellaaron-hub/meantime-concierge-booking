"""booking_events is the append-only audit log, and it must stay indexed.

It had no index at all beyond its primary key -- a Postgres foreign key
does not create one -- while every read of it is per-booking and
newest-first: Booking.events on every admin booking page,
documents.was_hand_edited, and the timeline reads that exist so nobody has
to reconstruct a booking from an email chain. Every write in the app adds
a row and nothing ever deletes one, so the table only grows.

Found on 2026-09-07 while reviewing an unrelated branch.
"""

from sqlalchemy import inspect

from app.models import BookingEvent

INDEX = "ix_booking_events_booking_created"


def test_the_audit_log_is_indexed_by_booking_newest_first():
    """Declared on the model, so a future table change carries it along."""
    declared = {index.name: [c.name for c in index.columns] for index in BookingEvent.__table__.indexes}
    assert INDEX in declared, "booking_events must stay indexed for its per-booking reads"
    assert declared[INDEX] == ["booking_id", "created_at"], (
        "leading column must be booking_id -- a created_at-only index does not serve "
        "the per-booking reads, and this one also covers a bare booking_id lookup"
    )


def test_the_index_exists_in_the_database(db):
    """The model and the migration have to agree: a declaration nothing
    created is worse than none, because it reads as covered."""
    names = {i["name"] for i in inspect(db.get_bind()).get_indexes("booking_events")}
    assert INDEX in names, (
        f"{INDEX} is declared on the model but missing from the database -- "
        "run alembic upgrade head"
    )
