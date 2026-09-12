"""CLI entry point for the iVvy parallel-run reconciliation report.

Usage: python -m app.run_ivvy_reconcile --venue <slug> "path/to/fresh_export.csv"

--venue is REQUIRED. Read-only, but a reconciliation run against the wrong
venue reports every booking as missing and every export row as new, which
reads as a catastrophe rather than as a mistyped argument.
"""

import sys

from app.database import SessionLocal
from app.services.ivvy_reconciliation import reconcile
from app.venue_arg import resolve_venue, take_venue_arg

USAGE = 'Usage: python -m app.run_ivvy_reconcile --venue <slug> <csv_path>'


def main(path: str, venue_slug: str | None) -> None:
    db = SessionLocal()
    try:
        venue = resolve_venue(db, venue_slug, usage=USAGE)
        report = reconcile(db, path, venue=venue)

        print(f"Matched and clean: {report.matched_clean}")
        print(f"New in iVvy, not yet in Concierge: {len(report.new_in_ivvy)}")
        for code in report.new_in_ivvy:
            print(f"  {code}")
        print(f"In Concierge but missing from this export: {len(report.missing_from_export)}")
        for code in report.missing_from_export:
            print(f"  {code}")
        print(f"Divergences: {len(report.divergences)}")
        for d in report.divergences:
            print(f"  {d.code} [{d.field}]: Concierge={d.concierge_value!r} iVvy={d.ivvy_value!r}")
        print(f"Row errors: {len(report.row_errors)}")
        for err in report.row_errors:
            print(f"  {err}")

        print()
        print("CLEAN -- safe to count toward the parallel-run period." if report.is_clean else "NOT CLEAN -- review above before counting this run.")
    finally:
        db.close()


if __name__ == "__main__":
    venue_slug, paths = take_venue_arg(sys.argv[1:])
    if len(paths) != 1:
        print(USAGE)
        sys.exit(1)
    main(paths[0], venue_slug)
