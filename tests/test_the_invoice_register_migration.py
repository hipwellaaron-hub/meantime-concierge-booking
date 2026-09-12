"""The invoice-register migration itself: the refusal, the backfill, the
seed, and the downgrade.

An adversarial review pointed out that nothing tested any of it -- the
register's BEHAVIOUR was covered but the one-way step that creates it was
not, so a regression in the migration would have shipped green. It also
found two real defects in it, both of which have a test here:

  * the downgrade restored `nextval('invoice_number_seq')` without moving
    the sequence past the numbers the counter had issued, so the restored
    insert path raised UniqueViolation once per invoice created since the
    cutover -- on the public deposit-invoice route, while the downgrade
    itself exited 0 and looked like a clean rollback; and
  * the insert trigger rebuilt `invoice_reference` on every insert,
    including the ones a pg_restore re-fires -- so restoring a dump taken
    before a prefix changed would silently rewrite every reference a client
    holds, defeating the whole reason it is stored rather than derived.

These drive the migration's real `upgrade()` and `downgrade()` functions
against the real Postgres, inside a transaction that is always rolled back.
The module is loaded by path because a revision filename is not importable.
"""
import datetime as dt
import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text

MIGRATION = pathlib.Path("alembic/versions/f3d9b7c1a468_each_venue_numbers_its_own_invoices.py")


def _migration():
    spec = importlib.util.spec_from_file_location("invoice_register_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pre_migration(db):
    """The schema as it stood BEFORE this migration, built inside the test
    transaction so the rollback puts everything back.

    Without this the probes would run against the already-migrated test
    database and prove nothing -- the vacuous-probe trap this project has
    hit twice.
    """
    db.execute(text("DROP TRIGGER IF EXISTS trg_invoices_freeze_identity ON invoices"))
    db.execute(text("DROP TRIGGER IF EXISTS trg_invoices_assign_number ON invoices"))
    db.execute(text("ALTER TABLE invoices DROP CONSTRAINT IF EXISTS uq_invoices_reference"))
    db.execute(text("ALTER TABLE invoices DROP CONSTRAINT IF EXISTS uq_invoices_venue_number"))
    db.execute(text("ALTER TABLE invoices DROP CONSTRAINT IF EXISTS fk_invoices_venue"))
    db.execute(text("ALTER TABLE invoices DROP COLUMN IF EXISTS invoice_reference"))
    db.execute(text("ALTER TABLE invoices DROP COLUMN IF EXISTS venue_id"))
    db.execute(text("DROP TABLE IF EXISTS venue_invoice_counters"))
    db.execute(text(
        "ALTER TABLE invoices ADD CONSTRAINT uq_invoices_invoice_number UNIQUE (invoice_number)"
    ))
    db.execute(text(
        "ALTER TABLE invoices ALTER COLUMN invoice_number "
        "SET DEFAULT nextval('invoice_number_seq')"
    ))
    db.flush()
    return db


def _run(db, fn):
    """Run upgrade()/downgrade() against the test transaction.

    alembic's op proxies to a MigrationContext; binding one to this
    session's connection is what lets the real function run inside the
    rollback.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(connection=db.connection())
    with Operations.context(ctx):
        fn()


def _venue(db, slug, prefix):
    from app.models import Venue

    venue = Venue(name=slug.title(), slug=slug, trading_name=slug.title(), reference_prefix=prefix)
    db.add(venue)
    db.flush()
    return venue


def _raw_invoice(db, venue, number):
    """An invoice row written directly, so a NUMBER can be chosen -- the
    pre-migration shape, where the column default supplied it."""
    from app.models import Space
    from app.services.booking import create_booking

    space = db.query(Space).filter_by(venue_id=venue.id).first()
    if space is None:
        space = Space(
            venue_id=venue.id, name=f"Room {uuid.uuid4().hex[:6]}", capacity=50,
            standard_min_adults=10, min_food_spend=0, is_bookable=True,
        )
        db.add(space)
        db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=60), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=f"Migration {number}", event_type="birthday",
        adult_count=20, child_count=0, notes=None, actor="test",
    )
    db.flush()
    db.execute(
        text(
            "INSERT INTO invoices (id, booking_id, type, line_items, subtotal, surcharge, "
            "total, status, due_date, access_token, invoice_number) VALUES "
            "(gen_random_uuid(), :b, 'deposit', '[]'::jsonb, 0, 0, 0, 'draft', :d, :t, :n)"
        ),
        {"b": booking.id, "d": dt.date.today(), "t": uuid.uuid4().hex, "n": number},
    )
    db.flush()
    return booking


# --- the refusal -----------------------------------------------------------


def test_it_refuses_when_two_venues_already_hold_invoices(pre_migration, db, hamilton):
    """Aaron's instruction, and the case an earlier design got silently and
    unrecoverably wrong: seeding both registers from one shared position
    put two companies' series into permanent lockstep."""
    other = _venue(db, f"second-{uuid.uuid4().hex[:6]}", "SEC")
    _raw_invoice(db, hamilton, 1004)
    _raw_invoice(db, other, 2001)

    with pytest.raises(RuntimeError) as exc:
        _run(db, _migration().upgrade)

    assert "already hold invoices" in str(exc.value)


def test_it_refuses_a_venue_holding_invoices_with_no_prefix(pre_migration, db, hamilton):
    """The reference cannot be built without one, and a blank prefix would
    make every backfilled reference read '-1004'."""
    _raw_invoice(db, hamilton, 1004)
    hamilton.reference_prefix = None
    db.flush()

    with pytest.raises(RuntimeError) as exc:
        _run(db, _migration().upgrade)

    assert "reference_prefix" in str(exc.value)


def test_one_venue_holding_invoices_is_not_refused(pre_migration, db, hamilton):
    """The other direction. A refusal that refused everything would pass
    both tests above while blocking the work entirely."""
    _raw_invoice(db, hamilton, 1004)

    _run(db, _migration().upgrade)

    assert db.execute(text("SELECT count(*) FROM venue_invoice_counters")).scalar() >= 1


# --- the backfill and the seed ---------------------------------------------


def test_every_existing_number_survives_and_gains_its_prefix(pre_migration, db, hamilton):
    """The one thing that must never change: a number a client already
    holds. Gaps included, because a register has them."""
    for number in (1001, 1002, 1005, 1017):
        _raw_invoice(db, hamilton, number)

    _run(db, _migration().upgrade)

    rows = db.execute(text(
        "SELECT invoice_number, invoice_reference FROM invoices ORDER BY invoice_number"
    )).all()
    assert [n for n, _ in rows] == [1001, 1002, 1005, 1017], "a number changed"
    assert [r for _, r in rows] == ["HAM-1001", "HAM-1002", "HAM-1005", "HAM-1017"]


def test_the_register_resumes_from_this_venues_own_max_not_the_sequence(
    pre_migration, db, hamilton
):
    """The defect that sank an earlier design. Made to differ on purpose:
    the sequence is burned far past the highest number actually issued, the
    way a rolled-back draft burns one."""
    _raw_invoice(db, hamilton, 1017)
    db.execute(text("SELECT setval('invoice_number_seq', 99999, true)"))
    db.flush()

    _run(db, _migration().upgrade)

    resumed = db.execute(text(
        "SELECT next_number FROM venue_invoice_counters c JOIN venues v ON v.id = c.venue_id "
        "WHERE v.slug = 'hamilton'"
    )).scalar()
    assert resumed == 1018, f"the register resumed from the retired sequence, not its own max: {resumed}"


def test_a_venue_with_no_invoices_starts_at_the_house_first_number(pre_migration, db, hamilton):
    other = _venue(db, f"fresh-{uuid.uuid4().hex[:6]}", "FRS")

    _run(db, _migration().upgrade)

    started = db.execute(
        text("SELECT next_number FROM venue_invoice_counters WHERE venue_id = :v"),
        {"v": other.id},
    ).scalar()
    assert started == 1001


# --- the downgrade ---------------------------------------------------------


def test_the_downgrade_leaves_a_database_that_can_still_issue_invoices(
    pre_migration, db, hamilton
):
    """THE defect the review found, and the reason this file exists.

    Nothing calls nextval once the migration drops the column default, so
    the sequence stands still while the counter climbs. Restoring the
    default without moving the sequence hands out numbers that are already
    taken -- UniqueViolation once per invoice issued since the cutover, on
    the public deposit-invoice route, while the downgrade itself exits 0.
    """
    _raw_invoice(db, hamilton, 1004)
    db.execute(text("SELECT setval('invoice_number_seq', 1004, true)"))
    db.flush()

    migration = _migration()
    _run(db, migration.upgrade)

    # Issue invoices the new way: the counter climbs, the sequence does not.
    for _ in range(3):
        _raw_invoice_via_counter(db, hamilton)
    before = db.execute(text("SELECT last_value FROM invoice_number_seq")).scalar()
    highest = db.execute(text("SELECT max(invoice_number) FROM invoices")).scalar()
    assert before < highest, "the probe is vacuous -- the sequence is already ahead"

    _run(db, migration.downgrade)

    after = db.execute(text("SELECT last_value FROM invoice_number_seq")).scalar()
    assert after >= highest, (
        f"the sequence was left at {after} behind the highest issued number {highest}; "
        "the restored insert path would collide on every invoice until it caught up"
    )


def _raw_invoice_via_counter(db, venue):
    """An invoice created the NEW way -- no number supplied, so the trigger
    allocates one from the register and the sequence is untouched."""
    from app.models import Space
    from app.services.booking import create_booking

    space = db.query(Space).filter_by(venue_id=venue.id).first()
    booking = create_booking(
        db, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=61), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=f"Post cutover {uuid.uuid4().hex[:6]}",
        event_type="birthday", adult_count=20, child_count=0, notes=None, actor="test",
    )
    db.flush()
    db.execute(
        text(
            "INSERT INTO invoices (id, booking_id, type, line_items, subtotal, surcharge, "
            "total, status, due_date, access_token) VALUES "
            "(gen_random_uuid(), :b, 'deposit', '[]'::jsonb, 0, 0, 0, 'draft', :d, :t)"
        ),
        {"b": booking.id, "d": dt.date.today(), "t": uuid.uuid4().hex},
    )
    db.flush()


# --- what a restore does ---------------------------------------------------


def test_a_restored_row_keeps_the_reference_it_arrives_with(db, hamilton, loft):
    """A pg_restore re-fires BEFORE INSERT triggers. Rebuilding the
    reference there would rewrite every historical one from the venue's
    CURRENT prefix -- silently changing what clients hold, which is the
    entire reason the reference is stored rather than derived.

    Runs against the migrated schema, because that is the state a restore
    lands in.
    """
    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=62), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="Restored Row", event_type="birthday",
        adult_count=20, child_count=0, notes=None, actor="test",
    )
    db.flush()

    db.execute(
        text(
            "INSERT INTO invoices (id, booking_id, type, line_items, subtotal, surcharge, "
            "total, status, due_date, access_token, invoice_number, invoice_reference) VALUES "
            "(gen_random_uuid(), :b, 'deposit', '[]'::jsonb, 0, 0, 0, 'draft', :d, :t, :n, :r)"
        ),
        {"b": booking.id, "d": dt.date.today(), "t": uuid.uuid4().hex,
         "n": 777, "r": "OLDPREFIX-777"},
    )
    db.flush()

    kept = db.execute(
        text("SELECT invoice_reference FROM invoices WHERE invoice_number = 777")
    ).scalar()
    assert kept == "OLDPREFIX-777", (
        f"a restored invoice's reference was rewritten to {kept!r} from the venue's current prefix"
    )


def test_a_new_invoice_still_gets_its_reference_built(db, hamilton, loft):
    """The other half: preserving a supplied reference must not stop the
    normal path building one."""
    from app.services import invoicing
    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=63), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="Built Reference", event_type="birthday",
        adult_count=20, child_count=0, notes=None, actor="test",
    )
    invoice = invoicing.create_deposit_invoice(
        db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test"
    )
    db.flush()

    assert invoice.invoice_reference == f"HAM-{invoice.invoice_number}"
