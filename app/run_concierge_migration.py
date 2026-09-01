"""CLI entry point for the one-time iVvy -> Concierge migration import.

Usage (runs against whatever DATABASE_URL points at -- dev locally):
    python -m app.run_concierge_migration "concierge-import.csv"

Prints a per-booking report: created (with new HAM reference, spaces,
deposit state, and any per-booking flags), plus skipped and errors.
"""

import sys

from app.database import SessionLocal
from app.models import Venue
from app.services.concierge_migration import import_migration_csv


def main(path: str) -> None:
    db = SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug="hamilton").one()
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
    if len(sys.argv) != 2:
        print("Usage: python -m app.run_concierge_migration <csv_path>")
        sys.exit(1)
    main(sys.argv[1])
