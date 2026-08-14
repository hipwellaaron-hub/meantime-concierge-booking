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
from sqlalchemy import text
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
        venue = db.query(Venue).filter_by(slug="hamilton").first()
        notification_failures = (
            len(enquiry_classification.get_enquiry_notification_failures(db, venue)) if venue else 0
        )
        checks = {
            "database": True,
            "gmail_configured": is_gmail_smtp_configured(),
            "stripe_configured": stripe_integration.is_configured(),
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
