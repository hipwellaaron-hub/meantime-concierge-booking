"""Does the database actually carry what the migrations promised?

Written because it turned out twice in one day that it did not, and both
times silently.

  * A pg_restore guard was added to assign_invoice_number() by editing the
    migration that creates it. That migration had already run, so alembic
    read the database as current and skipped the file. The fix shipped, was
    reviewed, had a passing test, and was not in the database.
  * invoices.paid_to_account was parented before the revision production
    was stamped at, so it could never have been created while the code that
    writes it was live.

Neither had a symptom anybody would notice until a restore or a payment.
The common shape is that ALEMBIC'S OPINION AND THE DATABASE HAD DIVERGED,
and nothing ever asked the database directly. This asks.

THREE KINDS OF QUESTION, and the split matters because only one of them can
be derived:

  1. TABLES AND COLUMNS come from Base.metadata, so they need no list and
     cannot go stale -- every model the application maps is compared with
     information_schema. That covers the paid_to_account class of fault for
     good.

  2. TRIGGERS AND FUNCTIONS are invisible to SQLAlchemy, so they are listed
     by hand below with the revision that created each and what breaks
     without it. A hand list rots, so
     tests/test_the_database_carries_what_the_migrations_promised.py
     compares this list against a really-migrated database and fails if a
     migration adds one and nobody adds it here.

  3. A BACKFILL'S RESULT is neither. d6b4e9f2a831 backfills
     staff_users.venue_id from a subquery, and a subquery that finds
     nothing returns NULL rather than failing -- so the migration could
     have emptied the column it was adding and exited 0. The guard that
     refuses that was added AFTER the migration was introduced, which is
     exactly the class above, so the result is checked rather than assumed.

Reported, never repaired. The same rule reconciliation states for itself,
and for the stronger reason here: the right repair for a missing trigger is
a migration somebody writes and reviews, not a CREATE issued by a health
check against a production database at three in the morning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import Base

# IMPORTED FOR THE SIDE EFFECT, and it is load-bearing. Base.metadata is
# populated by the act of importing the model modules, so a caller that has
# not imported them sees EMPTY metadata -- and _check_models then iterates
# nothing, finds nothing, and reports a clean schema. That is not a
# hypothetical: the first run of this module against concierge_dev returned
# "0 missing columns" while invoices.paid_to_account was genuinely absent,
# because app.models had never been imported in that process. A check whose
# silence means "I looked at nothing" is worse than no check.
import app.models  # noqa: F401

logger = logging.getLogger(__name__)

MISSING_TABLE = "missing_table"
MISSING_COLUMN = "missing_column"
MISSING_TRIGGER = "missing_trigger"
MISSING_FUNCTION = "missing_function"
STALE_FUNCTION = "stale_function"
BACKFILL_INCOMPLETE = "backfill_incomplete"


@dataclass(frozen=True)
class Problem:
    kind: str
    name: str
    detail: str

    def __str__(self) -> str:  # what lands in the log line
        return f"{self.kind}: {self.name} -- {self.detail}"


# name -> (table, revision that creates it, what breaks without it)
EXPECTED_TRIGGERS: dict[str, tuple[str, str, str]] = {
    "trg_booking_events_no_delete": (
        "booking_events", "0c9d43631866",
        "the audit trail stops being append-only and a row can be deleted",
    ),
    "trg_booking_events_no_update": (
        "booking_events", "0c9d43631866",
        "an audit row can be rewritten after the fact",
    ),
    "trg_bookings_venue_defaults_from_space": (
        "bookings", "d8c3f1a7e920",
        "a booking inserted without an explicit venue_id violates NOT NULL instead of "
        "taking the venue from its space. create_booking does pass one, so this is "
        "silent until something else inserts a booking",
    ),
    "trg_bookings_venue_is_immutable": (
        "bookings", "d8c3f1a7e920",
        "a booking can be moved between venues, which is the scoping guarantee itself",
    ),
    "trg_invoices_assign_number": (
        "invoices", "f3d9b7c1a468",
        "an invoice gets no venue, no number and no reference, and the register stops",
    ),
    "trg_invoices_freeze_identity": (
        "invoices", "f3d9b7c1a468",
        "an invoice number or reference a client is holding can be rewritten",
    ),
}

# name -> (revision, what breaks without it)
EXPECTED_FUNCTIONS: dict[str, tuple[str, str]] = {
    "assign_invoice_number": ("f3d9b7c1a468", "the invoice register cannot issue a number"),
    "fill_booking_venue_from_space": ("d8c3f1a7e920", "see trg_bookings_venue_defaults_from_space"),
    "freeze_invoice_identity": ("f3d9b7c1a468", "see trg_invoices_freeze_identity"),
    "prevent_booking_events_mutation": ("0c9d43631866", "see trg_booking_events_no_update"),
    "prevent_booking_venue_change": ("d8c3f1a7e920", "see trg_bookings_venue_is_immutable"),
}

# The case a name check cannot catch: the function is there under the right
# name and its BODY is an old version. Each entry is a fragment that must
# appear in pg_proc.prosrc, with what its absence means.
REQUIRED_FUNCTION_BODIES: dict[str, list[tuple[str, str]]] = {
    "assign_invoice_number": [
        (
            "IF NEW.invoice_reference IS NULL",
            "the pre-d5b3f7a20c91 body: it rebuilds invoice_reference on EVERY insert, "
            "and pg_restore re-fires BEFORE INSERT triggers -- so restoring a dump would "
            "rewrite every reference a client holds from the venue's current prefix",
        ),
    ],
    "fill_booking_venue_from_space": [
        (
            "IF NEW.venue_id IS NULL",
            "it would overwrite an explicitly supplied venue_id rather than only "
            "defaulting a missing one",
        ),
    ],
}


def _live_tables_and_columns(db: Session) -> dict[str, set[str]]:
    rows = db.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
    ).all()
    live: dict[str, set[str]] = {}
    for table, column in rows:
        live.setdefault(table, set()).add(column)
    return live


def _check_models(db: Session) -> list[Problem]:
    """Every mapped table and column must exist.

    ONE DIRECTION ONLY. A column in the database that no model maps is
    normal -- a rename leaves the old one behind until a later migration
    drops it, and flagging that would make this noisy enough to ignore.
    What cannot be tolerated is a model reading a column that is not there,
    because that is a 500 on whatever page touches it.
    """
    live = _live_tables_and_columns(db)
    problems: list[Problem] = []
    for table in Base.metadata.sorted_tables:
        columns = live.get(table.name)
        if columns is None:
            problems.append(Problem(
                MISSING_TABLE, table.name,
                "the application maps this table and the database has no such table",
            ))
            continue
        for column in table.columns:
            if column.name not in columns:
                problems.append(Problem(
                    MISSING_COLUMN, f"{table.name}.{column.name}",
                    "the application maps this column and the database has no such column; "
                    "every query against this table will fail",
                ))
    return problems


def observed_triggers(db: Session) -> dict[str, str]:
    """Every trigger this database really has, name -> table.

    Exposed rather than inlined so the test that keeps EXPECTED_TRIGGERS
    honest asks the SAME question the audit asks. A test that
    re-implements the query can drift from it and then agree with itself.
    """
    return {
        name: table
        for name, table in db.execute(
            text(
                "SELECT tgname, tgrelid::regclass::text FROM pg_trigger "
                "WHERE NOT tgisinternal"
            )
        ).all()
    }


def observed_functions(db: Session) -> dict[str, str]:
    """Every function this database really has, name -> body.

    Functions belonging to an EXTENSION are excluded. btree_gist alone
    installs about two hundred of them, and listing those as "unexpected"
    would bury the five that are ours.
    """
    return {
        name: src
        for name, src in db.execute(
            text(
                "SELECT p.proname, p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'"
                ")"
            )
        ).all()
    }


def _check_triggers(db: Session) -> list[Problem]:
    present = observed_triggers(db)
    problems: list[Problem] = []
    for name, (table, revision, consequence) in EXPECTED_TRIGGERS.items():
        if name not in present:
            problems.append(Problem(
                MISSING_TRIGGER, name,
                f"created by {revision} on {table}; without it, {consequence}",
            ))
        elif present[name] != table:
            problems.append(Problem(
                MISSING_TRIGGER, name,
                f"expected on {table}, found on {present[name]}",
            ))
    return problems


def _check_function_bodies(db: Session, bodies: dict[str, str]) -> list[Problem]:
    problems: list[Problem] = []
    for name, required in REQUIRED_FUNCTION_BODIES.items():
        src = bodies.get(name)
        if src is None:
            continue
        for fragment, consequence in required:
            if fragment not in src:
                problems.append(Problem(
                    STALE_FUNCTION, name,
                    f"its body is missing {fragment!r}, which means {consequence}",
                ))
    return problems


def _check_backfills(db: Session) -> list[Problem]:
    """d6b4e9f2a831's backfill, checked rather than assumed.

    A floor account with no venue is refused at sign-in by
    staff_auth.venue_for_token -- correctly, since NULL there means "every
    venue", which is what an ADMIN is and is not something a half-configured
    floor account may inherit. So admins with no venue are right and are not
    counted; floor accounts with none are locked out.
    """
    stranded = db.execute(
        text("SELECT count(*) FROM staff_users WHERE role = 'floor' AND venue_id IS NULL")
    ).scalar_one()
    if stranded:
        return [Problem(
            BACKFILL_INCOMPLETE, "staff_users.venue_id",
            f"{stranded} floor account(s) have no venue and cannot sign into the floor "
            "app at all; d6b4e9f2a831's backfill resolves a subquery that returns NULL "
            "rather than failing when the venue is absent",
        )]
    return []


def audit(db: Session) -> list[Problem]:
    """Every check. Reads only."""
    problems = _check_models(db)
    problems += _check_triggers(db)

    bodies = observed_functions(db)
    for name, (revision, consequence) in EXPECTED_FUNCTIONS.items():
        if name not in bodies:
            problems.append(Problem(
                MISSING_FUNCTION, name,
                f"created by {revision}; without it, {consequence}",
            ))
    problems += _check_function_bodies(db, bodies)
    problems += _check_backfills(db)
    return problems


def drifting(db: Session) -> bool:
    """The one boolean /healthz reports.

    The detail is LOGGED rather than returned, because /healthz is public
    and its own docstring forbids leaking anything but booleans and counts
    -- a list of the triggers this database is missing is a map of what is
    unguarded.
    """
    try:
        problems = audit(db)
    except Exception:
        logger.exception("schema audit could not run")
        # Cannot confirm the schema is sound, so do not claim it is.
        return True
    for problem in problems:
        logger.warning("schema drift -- %s", problem)
    return bool(problems)
