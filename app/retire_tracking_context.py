"""Clear stored client IP addresses and user agents, on a schedule of its
own.

    python -m app.retire_tracking_context            # clear what is due
    python -m app.retire_tracking_context --dry-run  # count, write nothing

WHY THIS IS ITS OWN ENTRYPOINT. The only code that ever deletes a client's
IP address and user agent is conversions.retire_tracking_context, and it
was reachable only from conversions.run_sweep -- which sits behind
TRACKING_SERVER_DISPATCH_ENABLED. So turning server-side ad dispatch OFF
also turned off the deletion, and the personal data it exists to remove
would sit in the database indefinitely. The switch that is supposed to
reduce what is held did the opposite.

Aaron, 2026-09-14: "make sure it runs independently of the dispatch gate,
since the whole point is that turning dispatch off shouldn't strand the
data."

So this job is gated on NOTHING. It does not read the dispatch flag, it
does not contact Meta or GA4, and it sends nothing anywhere -- it only
removes. Run it on a cron beside the digest and the reconciliation, and it
keeps working whatever the tracking switches say.

The cookie ids are deliberately left: they carry no more than the analytics
platforms already hold, and they are what a later reconciliation would need.
"""

import argparse
import datetime as dt
import logging
import sys

from app.database import SessionLocal
from app.services import conversions

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear expired client IP addresses and user agents")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report how many bookings would be cleared, and write nothing",
    )
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        if args.dry_run:
            due = conversions.count_tracking_context_due(db, now=now)
            print(f"{due} booking(s) hold a client IP or user agent past its retention window")
            return 0

        cleared = conversions.retire_tracking_context(db, now=now)
        db.commit()
        # Printed even when zero. A retention job that says nothing on a
        # quiet run is indistinguishable from one that did not run, and
        # this is the job whose whole purpose is that somebody can tell.
        print(f"Cleared client IP and user agent from {cleared} booking(s)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
