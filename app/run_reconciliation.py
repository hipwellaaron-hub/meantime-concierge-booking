"""Nightly reconciliation run (brief section 9).

Usage (runs against whatever DATABASE_URL points at):
    python -m app.run_reconciliation                    # every venue
    python -m app.run_reconciliation --venue hamilton   # just that one
    python -m app.run_reconciliation --dry-run

EVERY VENUE BY DEFAULT. This runs unattended on a cron, so the default has
to be the complete answer: a venue left out has an empty findings list in
triage, and an empty findings list looks exactly like a clean one.

--venue used to default to settings.ai_venue_slug, which is not a slug at
all -- it is the comma-separated list of venues the AI credential may read
(app/services/ai_access.py). With one venue that happened to work. With two
the lookup matches nothing and the job reconciles neither of them, exiting 1
into a cron log.

Reads everything and fixes nothing. --dry-run prints what it would open or
resolve without writing, which is how to check it against production data
before letting it run unattended.
"""

import argparse
import logging
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Venue
from app.services import reconciliation
from app.venue_arg import VenueArgumentError, resolve_venue

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the nightly reconciliation checks")
    parser.add_argument("--dry-run", action="store_true", help="report findings without writing")
    parser.add_argument(
        "--venue", default=None,
        help="one venue's slug; omit to run every venue (the unattended default)",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.venue:
            try:
                venues = [resolve_venue(db, args.venue, usage="python -m app.run_reconciliation --venue <slug>")]
            except VenueArgumentError as exc:
                print(exc)
                return 1
        else:
            venues = list(db.scalars(select(Venue).order_by(Venue.slug)).all())
            if not venues:
                # Not "nothing to do": a reconciliation that found no venue
                # to reconcile has not run, and saying so is the difference
                # between a quiet success and a quiet nothing.
                print("No venues in the database -- nothing was reconciled")
                return 1

        failed = False
        for venue in venues:
            # Each venue named on its own line, so a run that covered one
            # venue and not the other is visible in the cron log rather
            # than inferred from a total.
            print(f"--- {venue.slug} ---")
            try:
                _run_one(db, venue, dry_run=args.dry_run)
            except Exception:  # noqa: BLE001
                # One venue's failure must not cost the other its run.
                logging.exception("Reconciliation failed for venue %s", venue.slug)
                failed = True
        return 1 if failed else 0


def _run_one(db, venue: Venue, *, dry_run: bool) -> None:
        if dry_run:
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
            return

        result = reconciliation.run(db, venue)
        print(
            f"Reconciliation complete: {result.opened} opened, "
            f"{result.still_open} still open, {result.resolved} resolved"
        )


if __name__ == "__main__":
    sys.exit(main())
