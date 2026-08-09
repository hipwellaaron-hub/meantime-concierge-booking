"""CLI entry point for the iVvy CSV importer.

Usage: python -m app.run_ivvy_import "path/to/export.csv" ["path/to/another.csv" ...]
"""

import sys

from app.database import SessionLocal
from app.models import Venue
from app.services.ivvy_import import import_ivvy_csv


def main(paths: list[str]) -> None:
    db = SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug="hamilton").one()
        for path in paths:
            result = import_ivvy_csv(db, path, venue=venue)
            print(f"{path}: created={result.created} skipped_existing={result.skipped_existing} errors={len(result.errors)}")
            for err in result.errors:
                print(f"  row {err.row_number} ({err.code}): {err.reason}")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m app.run_ivvy_import <csv_path> [<csv_path> ...]")
        sys.exit(1)
    main(sys.argv[1:])
