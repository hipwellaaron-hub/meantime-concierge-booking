"""Ordering the audit trail must not depend on the query plan.

`created_at` is `server_default now()`, and Postgres `now()` is TRANSACTION
START time -- so every event written inside one transaction shares a
timestamp to the microsecond. Ordering by it is ordering on tied values, and
which row comes back first is then whatever plan the planner chose.

It held by accident: booking_events is append-only, so no row moves, so a
sequential scan returns heap order, which is insertion order. An index scan
returns index order instead, and the plan flips when the table's statistics
change -- which is exactly what a day of inserting and deleting rows does.

Seen once in four full-suite runs on 2026-09-12, in
test_end_to_end.py::test_full_journey, whose own comment had predicted it.
"""
import datetime as dt

from sqlalchemy import text

from app.models import BookingEvent


def _events(db, booking):
    return db.query(BookingEvent).filter_by(booking_id=booking.id).order_by(BookingEvent.seq).all()


def test_events_written_in_one_transaction_really_do_tie_on_created_at(db, booking):
    """The premise. If this ever stops being true -- a change to
    server_default, or clock_timestamp() instead of now() -- then the whole
    problem is gone and this file can go with it.
    """
    for i in range(5):
        db.add(BookingEvent(
            booking_id=booking.id, event_type="field_changed",
            field_name=f"probe_{i}", actor="test",
        ))
    db.flush()

    stamps = {e.created_at for e in _events(db, booking)}

    assert len(stamps) == 1, (
        f"events in one transaction no longer share a timestamp ({len(stamps)} distinct) -- "
        "ordering by created_at may now be safe, and this guard unnecessary"
    )


def test_seq_is_distinct_even_when_created_at_ties(db, booking):
    """The fix. Same transaction, same timestamp, different seq."""
    for i in range(5):
        db.add(BookingEvent(
            booking_id=booking.id, event_type="field_changed",
            field_name=f"ordered_{i}", actor="test",
        ))
    db.flush()

    events = _events(db, booking)
    seqs = [e.seq for e in events]

    assert len(set(seqs)) == len(seqs), f"seq values tied: {seqs}"
    assert seqs == sorted(seqs)


def test_the_order_is_the_order_they_were_written(db, booking):
    """What the audit trail is FOR: reconstructing how a booking got here
    without re-reading an email chain."""
    written = [f"step_{i}" for i in range(6)]
    for name in written:
        db.add(BookingEvent(
            booking_id=booking.id, event_type="field_changed", field_name=name, actor="test",
        ))
        db.flush()  # each its own statement, all inside one transaction

    got = [e.field_name for e in _events(db, booking) if e.field_name in written]

    assert got == written


def test_the_order_survives_an_index_scan(db, booking):
    """THE one that reproduces the flake.

    Ordering by created_at was correct only under a sequential scan. Forcing
    an index scan is what a statistics change does in production, and it is
    what made a green test go red once in four runs. Ordering by seq must be
    stable under both.
    """
    for i in range(8):
        db.add(BookingEvent(
            booking_id=booking.id, event_type="field_changed",
            field_name=f"plan_{i}", actor="test",
        ))
    db.flush()

    # field_name is nullable and the booking fixture's own "created" event
    # has none, so filter on the value being present before matching it.
    by_seq = [
        e.field_name for e in _events(db, booking)
        if e.field_name and e.field_name.startswith("plan_")
    ]

    # Same query, forced onto the index rather than a sequential scan.
    db.execute(text("SET LOCAL enable_seqscan = off"))
    forced = [
        r[0] for r in db.execute(text(
            "SELECT field_name FROM booking_events WHERE booking_id = :b "
            "AND field_name LIKE 'plan_%' ORDER BY seq"
        ), {"b": booking.id})
    ]
    db.execute(text("SET LOCAL enable_seqscan = on"))

    assert forced == by_seq, "the order changed with the plan"
    assert forced == [f"plan_{i}" for i in range(8)]


def test_the_relationship_orders_by_seq_too(db, booking):
    """booking.events is what most readers actually use."""
    for i in range(4):
        db.add(BookingEvent(
            booking_id=booking.id, event_type="field_changed",
            field_name=f"rel_{i}", actor="test",
        ))
    db.flush()
    db.refresh(booking)

    seqs = [e.seq for e in booking.events]

    assert seqs == sorted(seqs), "the relationship is not ordered by seq"


def test_the_newest_event_of_a_kind_is_unambiguous(db, booking):
    """Not a display problem. document_regeneration orders created_at DESC
    to find the LATEST event of a kind, and with ties that returned an
    arbitrary one of several -- so "was this regenerated since?" could be
    answered with the wrong row."""
    for i in range(5):
        db.add(BookingEvent(
            booking_id=booking.id, event_type="document_revised",
            field_name="beo_version", new_value=f"v{i}", actor="test",
        ))
    db.flush()

    newest = (
        db.query(BookingEvent)
        .filter_by(booking_id=booking.id, event_type="document_revised")
        .order_by(BookingEvent.seq.desc())
        .first()
    )

    assert newest.new_value == "v4"


def test_nothing_still_orders_this_table_by_created_at():
    """The sweep. A reader left on created_at keeps the old tie."""
    import pathlib

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "BookingEvent.created_at" in line and "order_by" in line:
                offenders.append(f"{path}:{n}")
            if 'order_by="BookingEvent.created_at"' in line:
                offenders.append(f"{path}:{n}")

    assert not offenders, f"these still order the audit log by a column that ties: {offenders}"
