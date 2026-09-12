"""Public, detailed health check -- powers the small status dot in the
top-right of the admin header, and is meant to also be polled by an
external uptime monitor (UptimeRobot, Better Uptime, Railway's own
healthcheck alerting, etc.). That second consumer is the important one:
an in-app widget can only ever report "the app looked fine the last time
someone with a tab open polled it" -- if the process crashes or the whole
service goes down, there is nobody left inside it to raise the alarm.
Detecting a genuine outage needs something watching from *outside* the
process, hitting this same endpoint, and paging a human when it stops
answering. See docs/ for that half of the setup.

Deliberately a separate route from the existing bare `/health` in
app/main.py, which Railway's own deploy tooling may depend on staying a
trivial, always-200, zero-dependency check -- this one does a real query
and is allowed to report something other than "ok".

Never returns anything beyond booleans/counts -- no config values, no
secrets, nothing that would turn a public monitoring endpoint into an
information leak.
"""

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Venue
from app.services import enquiry_classification, stripe_integration
from app.services.notifications import is_gmail_smtp_configured

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    if not database_ok:
        # The one check that matters most: if this fails, nothing else
        # below can be trusted either (it all needs the same connection).
        return {
            "status": "down",
            "checks": {"database": False},
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    try:
        # EVERY venue, not a hardcoded one. Two things were wrong here until
        # 2026-09-12, and they compounded:
        #
        #   * `filter_by(slug="hamilton").first()` -- `.first()` returns None
        #     where the eight `_venue()` helpers all use `.one()`, which
        #     raises. So a database with no Hamilton row reported
        #     notification_failures = 0 and status "ok". The one endpoint
        #     that exists to DETECT trouble was the one that failed open, and
        #     an external monitor would have seen a green light over an empty
        #     database.
        #   * Only Hamilton was ever asked. A second venue's failing enquiry
        #     notifications would not have shown up at all.
        #
        # No venue is NAMED in the response: this endpoint is public, its own
        # docstring forbids leaking config, and which companies operate here
        # is not a fact a monitoring URL should hand out. One folded boolean.
        venues = db.scalars(select(Venue)).all()
        if not venues:
            # A database with no venue cannot serve anybody. Degraded, loudly.
            return {
                "status": "degraded",
                "checks": {"database": True, "venues_present": False},
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        notification_failures = sum(
            len(enquiry_classification.get_enquiry_notification_failures(db, venue))
            for venue in venues
        )
        checks = {
            "database": True,
            "venues_present": True,
            "gmail_configured": is_gmail_smtp_configured(),
            # Per venue, folded: true only when EVERY venue could actually
            # mint a payment link. A process-level "some key is set" answer
            # would read green while a second company had no key at all.
            # is_configured_for only reads an environment variable -- never
            # assert_key_belongs_to, which calls Stripe over the network and
            # has no business inside a health check.
            "stripe_configured": all(
                stripe_integration.is_configured_for(venue) for venue in venues
            ),
            "enquiry_notifications_failing": notification_failures > 0,
        }
        status = "degraded" if notification_failures > 0 else "ok"
    except Exception:
        # A real DB connection but something else broke -- still report
        # what we could confirm rather than raising a 500 for a monitor
        # to interpret however it likes.
        checks = {"database": True}
        status = "degraded"

    return {"status": status, "checks": checks, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
