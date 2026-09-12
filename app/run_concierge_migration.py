"""CLI entry point for the one-time iVvy -> Concierge migration.

Usage (runs against whatever DATABASE_URL points at):
    python -m app.run_concierge_migration --venue <slug> --report "import.csv"   # READ-ONLY preview
    python -m app.run_concierge_migration --venue <slug> "import.csv"            # the real import

Always run --report first (safe to run against production) and eyeball the
"would create" and "possible duplicate" sections before the real import.

--venue is REQUIRED on both. This is the script that CREATES bookings in
bulk, and venue_id is immutable by database trigger once written -- a run
against the wrong venue is not fixed by an UPDATE, it is fixed by deleting
and re-importing every booking it made, after clients already hold their
reference codes.
"""

import sys

from app.database import SessionLocal
from app.services.concierge_migration import import_migration_csv, report_migration_csv
from app.venue_arg import resolve_venue, take_venue_arg

USAGE = 'Usage: python -m app.run_concierge_migration --venue <slug> [--report] <csv_path>'


def report(path: str, venue_slug: str | None) -> None:
    db = SessionLocal()
    try:
        venue = resolve_venue(db, venue_slug, usage=USAGE)
        rep = report_migration_csv(db, path, venue=venue)
    finally:
        db.close()

    print(f"\n=== WOULD CREATE: {len(rep.would_create)} booking(s) ===")
    for b in rep.would_create:
        linked = "  [LINKED]" if " + " in b["spaces"] else ""
        print(f"  {b['code']}  |  {b['event_name']}  |  {b['event_date']}  |  {b['spaces']}  |  {b['contact']}  |  deposit {b['deposit']}{linked}")
        for fl in b["flags"]:
            print(f"       ! {fl}")

    print(f"\n=== POSSIBLE DUPLICATES (hand-entered, no iVvy code) -- REVIEW: {len(rep.possible_duplicate)} ===")
    for note in rep.possible_duplicate:
        print(f"  {note}")

    print(f"\n=== EXCLUDED by code: {len(rep.excluded)} === {rep.excluded}")
    print(f"=== deposit UNKNOWN (skipped): {len(rep.unknown)} === {rep.unknown}")
    print(f"=== already imported: {len(rep.already_imported)} === {rep.already_imported}")
    print(f"\n=== ERRORS (not importable): {len(rep.errors)} ===")
    for code, reason in rep.errors:
        print(f"  {code}: {reason}")
    print(
        f"\nReport summary: would_create={len(rep.would_create)} "
        f"possible_duplicate={len(rep.possible_duplicate)} excluded={len(rep.excluded)} "
        f"unknown={len(rep.unknown)} already_imported={len(rep.already_imported)} errors={len(rep.errors)}"
    )
    print("\nREAD-ONLY report -- nothing was written.")


def main(path: str, venue_slug: str | None) -> None:
    db = SessionLocal()
    try:
        venue = resolve_venue(db, venue_slug, usage=USAGE)
        print(f"Importing into venue: {venue.trading_name or venue.name} ({venue.slug})")
        result = import_migration_csv(db, path, venue=venue)
    finally:
        db.close()

    print(f"\n=== CREATED: {len(result.created)} booking(s) ===")
    for b in result.created:
        spaces = " + ".join(b.spaces)
        linked = "  [LINKED]" if len(b.spaces) > 1 else ""
        print(f"  {b.booking_code}  ->  {b.reference_code}  |  {b.event_name}  |  {spaces}  |  deposit {b.deposit}{linked}")
        for flag in b.flags:
            print(f"       ! {flag}")

    print(f"\n=== SKIPPED (deposit UNKNOWN, not imported): {len(result.skipped_unknown)} ===")
    for code in result.skipped_unknown:
        print(f"  {code}")

    print(f"\n=== SKIPPED (excluded code, never imported): {len(result.skipped_excluded)} ===")
    for code in result.skipped_excluded:
        print(f"  {code}")

    print(f"\n=== SKIPPED (possible hand-entered duplicate -- reconcile by hand): {len(result.skipped_possible_duplicate)} ===")
    for note in result.skipped_possible_duplicate:
        print(f"  {note}")

    print(f"\n=== SKIPPED (already imported): {len(result.skipped_existing)} ===")
    for code in result.skipped_existing:
        print(f"  {code}")

    print(f"\n=== ERRORS: {len(result.errors)} ===")
    for code, reason in result.errors:
        print(f"  {code}: {reason}")

    print(
        f"\nSummary: created={len(result.created)} "
        f"skipped_unknown={len(result.skipped_unknown)} "
        f"skipped_excluded={len(result.skipped_excluded)} "
        f"skipped_possible_duplicate={len(result.skipped_possible_duplicate)} "
        f"skipped_existing={len(result.skipped_existing)} errors={len(result.errors)}"
    )


if __name__ == "__main__":
    venue_slug, args = take_venue_arg(sys.argv[1:])
    if len(args) == 2 and args[0] == "--report":
        report(args[1], venue_slug)
    elif len(args) == 1:
        main(args[0], venue_slug)
    else:
        print(USAGE)
        sys.exit(1)
