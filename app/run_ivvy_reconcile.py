"""CLI entry point for the iVvy parallel-run reconciliation report.

Usage: python -m app.run_ivvy_reconcile "path/to/fresh_export.csv"
"""

import sys

from app.database import SessionLocal
from app.models import Venue
from app.services.ivvy_reconciliation import reconcile


def main(path: str) -> None:
    db = SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug="hamilton").one()
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
    if len(sys.argv) != 2:
        print("Usage: python -m app.run_ivvy_reconcile <csv_path>")
        sys.exit(1)
    main(sys.argv[1])
