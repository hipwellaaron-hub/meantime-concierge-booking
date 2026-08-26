"""TEMPORARY. Exists only to run app.services.ivvy_calendar_import against
the production database from outside the running process -- this
environment has no shell/SQL access into the production container and no
production staff login. Gated by a shared secret (MAINTENANCE_SECRET)
compared with secrets.compare_digest, same pattern as the Stripe
webhook's signature check.

DELETE THIS FILE AND ITS ROUTER MOUNT IN app/main.py once the one-time
import run this exists for is confirmed done.
"""

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Venue
from app.services.ivvy_calendar_import import import_calendar_rows

router = APIRouter(tags=["internal-maintenance"])

MAINTENANCE_SECRET = os.environ.get("MAINTENANCE_SECRET")


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.post("/internal/maintenance/import-ivvy-calendar-rows")
def run_import_calendar_rows(request: Request, payload: dict, db: Session = Depends(get_db)):
    if not MAINTENANCE_SECRET:
        raise HTTPException(status_code=503, detail="Maintenance endpoint not configured")
    provided = request.headers.get("x-maintenance-secret", "")
    if not secrets.compare_digest(provided, MAINTENANCE_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="Body must be {'rows': [...]}")

    venue = _venue(db)
    result = import_calendar_rows(db, rows, venue=venue, actor="maintenance:import_ivvy_calendar")
    return {
        "created": result.created,
        "linked_spaces_added": result.linked_spaces_added,
        "skipped_existing": result.skipped_existing,
        "created_reference_codes": result.created_reference_codes,
        "errors": [{"booking_code": e.booking_code, "reason": e.reason} for e in result.errors],
    }
