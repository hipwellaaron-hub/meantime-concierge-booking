"""TEMPORARY. Exists only to trigger app.archive_bookings_before against
the production database from outside the running process -- this
environment has no shell/SQL access into the production container and no
production staff login, so a plain internal-only script invocation isn't
possible. Gated by a shared secret (MAINTENANCE_SECRET) compared with
secrets.compare_digest, the same pattern as the Stripe webhook's
signature check -- not session/CSRF auth, since this must be callable
from a plain HTTP client with no browser session.

DELETE THIS FILE AND ITS ROUTER MOUNT IN app/main.py once the one-time
archive run this exists for is confirmed done. This is not meant to be a
permanent feature -- a secret-gated, unauthenticated route that mutates
booking status is not something that should sit in production
indefinitely just because it's convenient.
"""

import datetime as dt
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.archive_bookings_before import archive_bookings_before
from app.database import get_db

router = APIRouter(tags=["internal-maintenance"])

MAINTENANCE_SECRET = os.environ.get("MAINTENANCE_SECRET")


@router.post("/internal/maintenance/archive-bookings-before")
def run_archive_bookings_before(request: Request, cutoff_date: str = Query(...), db: Session = Depends(get_db)):
    if not MAINTENANCE_SECRET:
        raise HTTPException(status_code=503, detail="Maintenance endpoint not configured")
    provided = request.headers.get("x-maintenance-secret", "")
    if not secrets.compare_digest(provided, MAINTENANCE_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        cutoff = dt.date.fromisoformat(cutoff_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="cutoff_date must be YYYY-MM-DD")

    archived = archive_bookings_before(db, cutoff, actor="maintenance:archive_pre_launch")
    return {
        "cutoff_date": str(cutoff),
        "archived_count": len(archived),
        "bookings": [
            {
                "reference_code": b.reference_code,
                "event_name": b.event_name,
                "event_date": str(b.event_date),
            }
            for b in archived
        ],
    }
