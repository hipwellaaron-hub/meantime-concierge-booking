"""A correction to an applied migration only lands if it is its own revision.

2026-09-14. Two faults, one cause: editing a migration that has already run.

THE TRIGGER FIX THAT NEVER RAN. f3d9b7c1a468 created
assign_invoice_number() with an unconditional
`NEW.invoice_reference := ...`. A review found the pg_restore hazard in it
and the guard was written into that same file's upgrade(). But the
migration had already run on production forty-five seconds into the
previous deploy, so alembic skipped the file and the guard never reached
the database. Deployment record, not reasoning:

    04:15:54  Running upgrade b7e4a91c3f20 -> f3d9b7c1a468   (pre-fix)
    04:25:47  Context impl PostgresqlImpl ... and no upgrade line at all

AND THE EXISTING TEST DID NOT CATCH IT, which is the part worth writing
down. test_the_invoice_register_migration.py already asserts a restored row
keeps its reference -- and it passes, because the TEST database was built
after the fix and therefore carries the guarded body. The assertion is
satisfied by the environment rather than by anything that reached
production. Same shape as the Stripe preview test earlier the same day:
something other than the thing under test was deciding the result.

So the probes here install the UNGUARDED body first, inside the test
transaction, and prove the damage before proving the repair. Nothing the
ambient database happens to carry can decide them.

THE STRANDED COLUMN. c2f8d61a94b7 (invoices.paid_to_account) was written
between b7e4a91c3f20 and f3d9b7c1a468, then a local history reorder pushed
f3d9b7c1a468 without it. Production stamped f3d9b7c1a468 -- which alembic
reads as "this and everything before it" -- so the column would never have
been created, while the code that writes it shipped. It is re-parented to
sit AFTER f3d9b7c1a468 now, and
test_a_database_at_the_register_migration_has_more_to_do is what fails if
anyone puts it back in front.
"""
import datetime as dt
import importlib.util
import pathlib
import re
import uuid

import pytest
from sqlalchemy import text

REGISTER = pathlib.Path("alembic/versions/f3d9b7c1a468_each_venue_numbers_its_own_invoices.py")
REPAIR = pathlib.Path(
    "alembic/versions/d5b3f7a20c91_the_invoice_trigger_stops_rebuilding_a_reference.py"
)

BLOCK = re.compile(
    r'op\.execute\(\s*f"""(.*?CREATE OR REPLACE FUNCTION assign_invoice_number.*?)"""\s*\)',
    re.S,
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(db, fn):
    """Run the revision's real upgrade() inside the test transaction.

    alembic's op proxies to a MigrationContext; binding one to this
    session's connection is what lets the real function run inside the
    rollback. Same helper as test_the_invoice_register_migration.py, and
    the point of using it rather than executing the SQL constant directly
    is that upgrade() itself is then what the assertion depends on.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(connection=db.connection())
    with Operations.context(ctx):
        fn()


# --- the copy cannot drift --------------------------------------------------


def test_the_repair_installs_the_same_function_the_register_migration_does():
    """The repair carries the body verbatim rather than importing it, so a
    later edit to f3d9b7c1a468 cannot retroactively change what this
    revision did on the day it ran. The cost of a copy is drift, and this
    is what pays it.

    Not hypothetical: the first draft of the repair was typed by hand and
    dropped `NEW.venue_id` from the counter INSERT, which would have raised
    on the first invoice for a venue with no register yet.
    """
    repair = _load(REPAIR, "trigger_repair")
    match = BLOCK.search(REGISTER.read_text(encoding="utf-8"))
    assert match, "the register migration no longer contains an assign_invoice_number block"

    expected = match.group(1).replace("{FIRST_NUMBER}", "1001")

    assert repair.GUARDED_ASSIGN_INVOICE_NUMBER == expected, (
        "the repair migration's copy of assign_invoice_number has drifted from "
        "the register migration's"
    )


def test_the_repair_body_carries_the_guard_and_the_counter_insert():
    """A positive control on the comparison above: if both files lost the
    guard together the equality would still hold and prove nothing."""
    repair = _load(REPAIR, "trigger_repair_controls")
    sql = repair.GUARDED_ASSIGN_INVOICE_NUMBER

    assert "IF NEW.invoice_reference IS NULL THEN" in sql
    assert "VALUES (NEW.venue_id, 1001 + 1)" in sql
    assert "{" not in sql, "an f-string placeholder survived into the executed SQL"


# --- and it lands on a database that already ran the register migration -----


@pytest.fixture()
def unguarded_trigger(db):
    """PRODUCTION'S ACTUAL STATE on 2026-09-14, rebuilt inside the test
    transaction: assign_invoice_number() as f3d9b7c1a468 first shipped it,
    rebuilding the reference on every insert.

    Derived from the repair's own body by deleting exactly the guard, so it
    cannot drift away from the function it is meant to be the older version
    of. Cross-checked against concierge_dev's pg_proc, which is running the
    real pre-fix body.
    """
    repair = _load(REPAIR, "trigger_repair_fixture")
    guarded_tail = (
        "            IF NEW.invoice_reference IS NULL THEN\n"
        "                NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;\n"
        "            END IF;"
    )
    assert guarded_tail in repair.GUARDED_ASSIGN_INVOICE_NUMBER, (
        "the guard is not in the shape this fixture knows how to remove"
    )
    unguarded = repair.GUARDED_ASSIGN_INVOICE_NUMBER.replace(
        guarded_tail,
        "            NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;",
    )
    assert "IF NEW.invoice_reference IS NULL" not in unguarded

    db.execute(text(unguarded))
    db.flush()
    yield


def _restored_invoice(db, loft, *, number, reference):
    """The shape a pg_restore inserts: a row arriving with its own identity."""
    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=64), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=f"Restored {number}", event_type="birthday",
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
         "n": number, "r": reference},
    )
    db.flush()
    return db.execute(
        text("SELECT invoice_reference FROM invoices WHERE invoice_number = :n"), {"n": number}
    ).scalar()


def test_the_defect_is_real_on_the_body_production_is_running(
    db, hamilton, loft, unguarded_trigger
):
    """Before the repair. If this passed, the repair would have nothing to
    fix and every assertion below it would be vacuous -- which is precisely
    how the existing register test came to be green while production was
    not fixed."""
    kept = _restored_invoice(db, loft, number=8801, reference="OLDPREFIX-8801")

    assert kept == "HAM-8801", (
        "the unguarded body did not rewrite the reference, so this fixture is not "
        f"reproducing production's defect (got {kept!r})"
    )


def test_the_repair_fixes_a_database_that_already_ran_the_register_migration(
    db, hamilton, loft, unguarded_trigger
):
    """THE one. Same starting state as the test above -- the body that is
    on production right now -- and the only thing that changes is running
    the repair revision's upgrade()."""
    repair = _load(REPAIR, "trigger_repair_apply")
    _run(db, repair.upgrade)
    db.flush()

    kept = _restored_invoice(db, loft, number=8802, reference="OLDPREFIX-8802")

    assert kept == "OLDPREFIX-8802", (
        f"the repair did not take: a restored reference was rewritten to {kept!r}"
    )


def test_the_repair_leaves_the_normal_path_building_a_reference(
    db, hamilton, loft, unguarded_trigger
):
    """The other half. A guard that preserves a supplied reference must not
    stop an ordinary new invoice getting one."""
    from app.models.invoice import InvoiceType
    from app.services import invoicing
    from app.services.booking import create_booking

    repair = _load(REPAIR, "trigger_repair_normal")
    _run(db, repair.upgrade)
    db.flush()

    booking = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=66), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="Ordinary Invoice", event_type="birthday",
        adult_count=20, child_count=0, notes=None, actor="test",
    )
    db.flush()
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today() + dt.timedelta(days=7), actor="test",
    )
    db.flush()

    assert invoice.invoice_reference == f"HAM-{invoice.invoice_number}"


# --- the chain --------------------------------------------------------------


def test_a_database_at_the_register_migration_has_more_to_do():
    """The stranded-column guard.

    Production is stamped f3d9b7c1a468. Alembic reads a stamp as "this and
    everything before it", so anything parented BEFORE that revision can
    never run there again -- which is exactly how invoices.paid_to_account
    came to be written by shipped code and absent from the database.

    Re-parenting c2f8d61a94b7 to sit after f3d9b7c1a468 is what makes it
    reachable. This fails if anyone moves it back in front.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))

    ancestors = {r.revision for r in script.iterate_revisions("f3d9b7c1a468", "base")}
    assert "c2f8d61a94b7" not in ancestors, (
        "c2f8d61a94b7 is parented before f3d9b7c1a468, so a database stamped "
        "f3d9b7c1a468 -- which production is -- would never create paid_to_account"
    )
    assert "b7e4a91c3f20" in ancestors, (
        "the register migration no longer descends from b7e4a91c3f20, which is what "
        "production actually ran it on top of"
    )

    pending = {r.revision for r in script.iterate_revisions("heads", "f3d9b7c1a468")}
    pending.discard("f3d9b7c1a468")
    assert "c2f8d61a94b7" in pending
    assert "d5b3f7a20c91" in pending
