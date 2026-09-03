"""Nightly reconciliation run (brief section 9).

Usage (runs against whatever DATABASE_URL points at):
    python -m app.run_reconciliation
    python -m app.run_reconciliation --dry-run

Reads everything and fixes nothing. --dry-run prints what it would open or
resolve without writing, which is how to check it against production data
before letting it run unattended.
"""

import argparse
import logging
import sys

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Venue
from app.services import reconciliation

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the nightly reconciliation checks")
    parser.add_argument("--dry-run", action="store_true", help="report findings without writing")
    parser.add_argument("--venue", default=settings.ai_venue_slug, help="venue slug")
    args = parser.parse_args()

    with SessionLocal() as db:
        venue = db.scalar(select(Venue).where(Venue.slug == args.venue))
        if venue is None:
            print(f"No venue with slug {args.venue!r}")
            return 1

        if args.dry_run:
            findings = reconciliation.collect(db, venue)
            print(f"{len(findings)} finding(s) -- nothing written")
            by_check: dict[str, int] = {}
            for f in findings:
                by_check[f.check_code] = by_check.get(f.check_code, 0) + 1
            for code, count in sorted(by_check.items()):
                print(f"  {code:<28} {count}")
            for f in findings:
                print(f"  - [{f.check_code}] {f.detail}")
            return 0

        result = reconciliation.run(db, venue)
        print(
            f"Reconciliation complete: {result.opened} opened, "
            f"{result.still_open} still open, {result.resolved} resolved"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
