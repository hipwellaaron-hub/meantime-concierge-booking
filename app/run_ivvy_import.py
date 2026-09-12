"""CLI entry point for the iVvy CSV importer.

Usage: python -m app.run_ivvy_import --venue <slug> "path/to/export.csv" [...]

--venue is REQUIRED. This used to hardcode Hamilton, which means it imported
whatever CSV you handed it into whichever venue somebody wrote into the
source -- invisible with one venue, and an import of one company's functions
into the other's books with two. bookings.venue_id is immutable by database
trigger, so the remedy would be delete-and-recreate, not an UPDATE.
"""

import sys

from app.database import SessionLocal
from app.services.ivvy_import import import_ivvy_csv
from app.venue_arg import resolve_venue, take_venue_arg

USAGE = 'Usage: python -m app.run_ivvy_import --venue <slug> <csv_path> [<csv_path> ...]'


def main(paths: list[str], venue_slug: str | None) -> None:
    db = SessionLocal()
    try:
        venue = resolve_venue(db, venue_slug, usage=USAGE)
        for path in paths:
            result = import_ivvy_csv(db, path, venue=venue)
            print(f"{path}: created={result.created} skipped_existing={result.skipped_existing} errors={len(result.errors)}")
            for err in result.errors:
                print(f"  row {err.row_number} ({err.code}): {err.reason}")
    finally:
        db.close()


if __name__ == "__main__":
    venue_slug, paths = take_venue_arg(sys.argv[1:])
    if not paths:
        print(USAGE)
        sys.exit(1)
    main(paths, venue_slug)
