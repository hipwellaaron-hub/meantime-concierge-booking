"""The schema audit, and the thing that keeps its hand-written lists honest.

2026-09-14. Two defects reached production because a fix was made by
editing a migration that had already run, and nothing ever asked the
database whether the fix was in it. Both had a green test and a reviewed
diff. Applied state is its own question.

EVERY PROBE HERE BREAKS THE DATABASE FIRST. That is not ceremony. The
shared test database is migrated to head, so an audit run against it
returns clean whatever the audit does -- including if it does nothing at
all, which is exactly how the first version of _check_models passed while
concierge_dev was genuinely missing invoices.paid_to_account (Base.metadata
is only populated by importing the model modules, and nothing had). So each
probe removes the real object inside the test transaction, proves the audit
sees it, and lets the rollback put it back.
"""
import pytest
from sqlalchemy import text

from app.services import schema_audit


@pytest.fixture()
def clean(db):
    """The audit must be able to say 'nothing wrong' on a migrated database.

    A check that reports a problem unconditionally would make every probe
    below pass without meaning anything.
    """
    problems = schema_audit.audit(db)
    assert problems == [], f"the test database is not clean to start with: {problems}"
    return db


# --- the audit must be looking at something ---------------------------------


def test_importing_only_the_audit_module_populates_the_model_metadata():
    """IN A SUBPROCESS, and that is the whole point of the test.

    Base.metadata is filled by the act of importing the model modules. If
    schema_audit does not import them itself, a process that has not
    imported them elsewhere sees EMPTY metadata -- and _check_models then
    iterates nothing, finds nothing, and reports a clean schema. That
    happened: the first run against concierge_dev said "no missing columns"
    while invoices.paid_to_account was genuinely absent.

    It cannot be caught in-process. Pytest's conftest imports the whole
    application before any test runs, so by the time an assertion executes
    the metadata is populated whatever schema_audit does -- the environment
    would satisfy the assertion rather than the code. A fresh interpreter
    that imports nothing but this module is the only thing that can decide
    it.
    """
    import subprocess
    import sys

    probe = (
        "from app.services import schema_audit;"
        "print(len(schema_audit.Base.metadata.sorted_tables))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd="."
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0, (
        "importing schema_audit alone leaves Base.metadata empty, so the audit would "
        "compare nothing against the database and report every schema as clean"
    )


# --- the lists cannot rot ---------------------------------------------------


def test_the_expected_triggers_are_exactly_what_a_migrated_database_has(clean):
    """EXPECTED_TRIGGERS is written by hand, so it rots the moment a
    migration adds a trigger and nobody adds it here -- and a list that has
    quietly stopped covering the newest thing is worse than no list, because
    it still reads as complete.

    Compared BOTH WAYS against a really-migrated database: a trigger listed
    and absent means the list is wrong about this revision; a trigger
    present and unlisted means the audit is not watching it.
    """
    observed = set(schema_audit.observed_triggers(clean))
    expected = set(schema_audit.EXPECTED_TRIGGERS)

    assert expected - observed == set(), (
        "EXPECTED_TRIGGERS names triggers a fully migrated database does not have"
    )
    assert observed - expected == set(), (
        "this database has triggers the audit does not watch -- add them to "
        "EXPECTED_TRIGGERS with the revision that creates them and what breaks without"
    )


def test_the_expected_functions_are_exactly_what_a_migrated_database_has(clean):
    observed = set(schema_audit.observed_functions(clean))
    expected = set(schema_audit.EXPECTED_FUNCTIONS)

    assert expected - observed == set(), (
        "EXPECTED_FUNCTIONS names functions a fully migrated database does not have"
    )
    assert observed - expected == set(), (
        "this database has functions the audit does not watch -- note that extension "
        "functions are already excluded, so these are ours"
    )


def test_the_expected_indexes_are_all_on_a_migrated_database(clean):
    """One direction only, unlike the triggers and functions. Postgres
    creates an index for every primary key and unique constraint and every
    Index() a model declares, so "present and unlisted" is the normal state
    of dozens of them. What EXPECTED_INDEXES holds is the handful whose
    absence costs money, and each must really exist.

    This is not theoretical cover: the first run of the index check against
    concierge_dev found uq_payments_stripe_payment_intent genuinely absent
    on a database whose alembic_version said d5b3f7a20c91 -- the exact
    divergence this module exists to catch, on the first database it was
    pointed at.
    """
    observed = schema_audit.observed_indexes(clean)

    missing = set(schema_audit.EXPECTED_INDEXES) - observed
    assert missing == set(), f"EXPECTED_INDEXES names indexes this database does not have: {missing}"


def test_a_missing_index_is_caught(clean):
    """THE money one: without it a redelivered Stripe webhook can record
    the same PaymentIntent twice against an invoice."""
    clean.execute(text("DROP INDEX uq_payments_stripe_payment_intent"))
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.MISSING_INDEX
        and p.name == "uq_payments_stripe_payment_intent"
        for p in problems
    ), f"a dropped unique index was not reported: {problems}"


def test_each_expected_trigger_is_recorded_on_the_right_table(clean):
    observed = schema_audit.observed_triggers(clean)
    for name, (table, _revision, _why) in schema_audit.EXPECTED_TRIGGERS.items():
        assert observed[name] == table, f"{name} is on {observed[name]}, not {table}"


def test_every_body_fragment_really_appears_in_the_live_function(clean):
    """REQUIRED_FUNCTION_BODIES is matched as a substring against
    pg_proc.prosrc. A fragment with a typo, or one reworded when the
    function was, would never match -- so the audit would report the
    function stale on a correct database, or (once someone "fixed" that by
    loosening it) never report anything at all."""
    bodies = schema_audit.observed_functions(clean)
    for name, required in schema_audit.REQUIRED_FUNCTION_BODIES.items():
        assert name in bodies, f"{name} is not a function on this database"
        for fragment, _why in required:
            assert fragment in bodies[name], (
                f"{fragment!r} does not appear in the live {name}, so this entry can "
                "only ever produce a false positive"
            )


# --- and it detects each thing it claims to ---------------------------------


def test_a_missing_column_is_caught(clean):
    """THE paid_to_account shape: the model maps it, the database does not,
    and every query against that table would fail."""
    clean.execute(text("ALTER TABLE invoices DROP COLUMN paid_to_account"))
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.MISSING_COLUMN and p.name == "invoices.paid_to_account"
        for p in problems
    ), f"a dropped mapped column was not reported: {problems}"


def test_a_missing_table_is_caught(clean):
    clean.execute(text("DROP TABLE venue_invoice_counters CASCADE"))
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.MISSING_TABLE and p.name == "venue_invoice_counters"
        for p in problems
    ), f"a dropped mapped table was not reported: {problems}"


def test_a_missing_trigger_is_caught(clean):
    """The d8c3f1a7e920 shape, and the reason it needs a check at all:
    create_booking passes venue_id explicitly, so this trigger's absence
    has no symptom in ordinary use."""
    clean.execute(
        text("DROP TRIGGER trg_bookings_venue_defaults_from_space ON bookings")
    )
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.MISSING_TRIGGER
        and p.name == "trg_bookings_venue_defaults_from_space"
        for p in problems
    ), f"a dropped trigger was not reported: {problems}"


def test_a_function_with_an_old_body_is_caught(clean):
    """THE one this module was written for. The function is present, under
    the right name, on the right trigger -- and its body is the version
    from before the fix. A name check cannot see this."""
    guarded = schema_audit.observed_functions(clean)["assign_invoice_number"]
    unguarded = guarded.replace(
        "IF NEW.invoice_reference IS NULL THEN\n"
        "                NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;\n"
        "            END IF;",
        "NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;",
    )
    assert "IF NEW.invoice_reference IS NULL" not in unguarded, (
        "the probe failed to remove the guard, so it is not reproducing the defect"
    )
    clean.execute(
        text(
            "CREATE OR REPLACE FUNCTION assign_invoice_number() RETURNS trigger AS $body$"
            + unguarded
            + "$body$ LANGUAGE plpgsql"
        )
    )
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.STALE_FUNCTION and p.name == "assign_invoice_number"
        for p in problems
    ), f"a function reverted to its pre-fix body was not reported: {problems}"


def test_a_present_function_with_the_current_body_is_not_reported(clean):
    """The other direction: STALE_FUNCTION must not fire on a correct
    database, or it is noise that trains people to ignore the endpoint."""
    assert not any(p.kind == schema_audit.STALE_FUNCTION for p in schema_audit.audit(clean))


# --- the backfill ------------------------------------------------------------


def _staff(db, hamilton, *, role, venue_id, email):
    from app.models import StaffUser

    row = StaffUser(
        email=email, name=email.split("@")[0], role=role, venue_id=venue_id,
        password_hash="x", is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def test_a_floor_account_with_no_venue_is_caught(clean, hamilton):
    """d6b4e9f2a831's backfill resolves `(SELECT id FROM venues WHERE
    slug='hamilton')`, and a subquery that finds nothing returns NULL
    rather than failing -- so it could set every row to NULL and exit 0.
    That account cannot sign into the floor app at all."""
    _staff(clean, hamilton, role="floor", venue_id=None, email="zzaudit-floor@local.test")

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.BACKFILL_INCOMPLETE and p.name == "staff_users.venue_id"
        for p in problems
    ), f"a venueless floor account was not reported: {problems}"


def test_a_device_token_with_no_venue_is_caught(clean, hamilton):
    """THE HALF THAT LOCKS THE PHONES OUT. d6b4e9f2a831 writes venue_id on
    staff_users AND on staff_app_tokens from the same NULL-returning
    subquery; the first version of this check asked only about the users,
    so it would have reported a clean database while every device token was
    venueless and every phone refused at sign-in."""
    from app.models import StaffAppToken

    staff = _staff(clean, hamilton, role="floor", venue_id=hamilton.id, email="zzaudit-dev@local.test")
    clean.add(StaffAppToken(
        staff_user_id=staff.id, venue_id=None, token_hash="zz-audit-probe",
    ))
    clean.flush()

    problems = schema_audit.audit(clean)

    assert any(
        p.kind == schema_audit.BACKFILL_INCOMPLETE and p.name == "staff_app_tokens.venue_id"
        for p in problems
    ), f"a venueless device token was not reported: {problems}"


def test_an_admin_with_no_venue_is_correct_and_not_reported(clean, hamilton):
    """NULL on an ADMIN means every venue -- that is what an admin is, and
    staff_auth.venue_for_token branches on ROLE for exactly this reason.
    Counting admins here would report a permanent false problem and the
    endpoint would sit degraded forever."""
    _staff(clean, hamilton, role="admin", venue_id=None, email="zzaudit-admin@local.test")

    problems = schema_audit.audit(clean)

    assert not any(p.kind == schema_audit.BACKFILL_INCOMPLETE for p in problems), (
        "an admin with no venue was reported as a stranded backfill"
    )


# --- what /healthz does with it ---------------------------------------------


def test_drifting_is_false_on_a_migrated_database(clean):
    assert schema_audit.drifting(clean) is False


def test_drifting_is_true_when_something_is_missing(clean):
    clean.execute(text("ALTER TABLE invoices DROP COLUMN paid_to_account"))
    clean.flush()

    assert schema_audit.drifting(clean) is True


def test_drifting_fails_closed_when_the_audit_itself_raises(monkeypatch, clean):
    """A check that returns 'fine' when it could not run is the failure mode
    /healthz already had once: .first() returning None made the endpoint
    report ok over an empty database. Not being able to confirm the schema
    is sound is not the same as it being sound."""
    def _boom(_db):
        raise RuntimeError("no connection")

    monkeypatch.setattr(schema_audit, "audit", _boom)

    assert schema_audit.drifting(clean) is True


def test_the_detail_is_logged_and_not_returned(caplog, clean):
    """The endpoint is public and its own docstring forbids leaking
    anything but booleans and counts. A list of the triggers a database is
    missing is a map of what is unguarded."""
    import logging

    clean.execute(text("DROP TRIGGER trg_invoices_freeze_identity ON invoices"))
    clean.flush()

    with caplog.at_level(logging.WARNING, logger="app.services.schema_audit"):
        result = schema_audit.drifting(clean)

    assert result is True
    assert any(
        "trg_invoices_freeze_identity" in r.getMessage() for r in caplog.records
    ), "the detail never reached the log, so nobody can act on the boolean"


def test_healthz_reports_schema_drift(clean, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: clean
    try:
        body = TestClient(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()

    assert "schema_drift" in body["checks"], "/healthz does not report the schema at all"
    assert body["checks"]["schema_drift"] is False


def test_healthz_goes_degraded_and_names_no_object(clean, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    clean.execute(text("DROP TRIGGER trg_invoices_assign_number ON invoices"))
    clean.flush()

    app.dependency_overrides[get_db] = lambda: clean
    try:
        response = TestClient(app).get("/healthz")
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert body["checks"]["schema_drift"] is True
    assert body["status"] == "degraded"
    assert "trg_invoices_assign_number" not in response.text, (
        "the public endpoint leaked which object is missing"
    )
