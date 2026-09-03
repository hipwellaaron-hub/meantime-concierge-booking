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
            from app.models import Booking

            by_id = {}
            for f in findings:
                if f.booking_id not in by_id:
                    by_id[f.booking_id] = db.get(Booking, f.booking_id)
            for f in findings:
                b = by_id.get(f.booking_id)
                who = f"{b.reference_code} {b.event_name}" if b else str(f.booking_id)
                print(f"  - [{f.check_code}] {who}: {f.detail}")
                if f.check_code == "NOTES_BEFORE_BEO" and b is not None:
                    # The whole point of this check is reading the text.
                    if b.enquiry_text:
                        print("      client wrote: " + b.enquiry_text.replace("\n", "\n                    "))
                    if b.notes:
                        print("      internal:     " + b.notes.replace("\n", "\n                    "))
            return 0

        result = reconciliation.run(db, venue)
        print(
            f"Reconciliation complete: {result.opened} opened, "
            f"{result.still_open} still open, {result.resolved} resolved"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
