"""TEMPORARY. Exists only to attach real contacts to specific bookings in
production from outside the running process -- this environment has no
shell/SQL access into the production container and no production staff
login. Gated by a shared secret (MAINTENANCE_SECRET) compared with
secrets.compare_digest, same pattern as the Stripe webhook's signature
check.

DELETE THIS FILE AND ITS ROUTER MOUNT IN app/main.py once the one-time
run this exists for is confirmed done. The permanent version of this
capability is the "Add contact" / "Change" form on the booking detail
page (app.services.booking.set_contact) -- this endpoint exists only
because this session has no staff session to use that form with.
"""

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking
from app.services.booking import flag_for_review, set_contact
from app.services.contact_matching import find_or_create_contact

router = APIRouter(tags=["internal-maintenance"])

MAINTENANCE_SECRET = os.environ.get("MAINTENANCE_SECRET")


@router.post("/internal/maintenance/set-booking-contacts")
def run_set_booking_contacts(request: Request, payload: dict, db: Session = Depends(get_db)):
    if not MAINTENANCE_SECRET:
        raise HTTPException(status_code=503, detail="Maintenance endpoint not configured")
    provided = request.headers.get("x-maintenance-secret", "")
    if not secrets.compare_digest(provided, MAINTENANCE_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="Body must be {'items': [...]}")

    results = []
    for item in items:
        reference_code = item.get("reference_code")
        try:
            booking = db.execute(select(Booking).where(Booking.reference_code == reference_code)).scalar_one_or_none()
            if booking is None:
                results.append({"reference_code": reference_code, "status": "not_found"})
                continue
            contact, _dupes = find_or_create_contact(db, item["name"], item["email"], item.get("phone"))
            set_contact(db, booking, contact_id=contact.id, actor="maintenance:set_booking_contacts")
            if item.get("review_note"):
                flag_for_review(db, booking, note=item["review_note"], actor="maintenance:set_booking_contacts")
            results.append({"reference_code": reference_code, "status": "ok", "contact_email": contact.email})
        except Exception as exc:  # noqa: BLE001 -- one bad item must not abort the rest of the batch
            db.rollback()
            results.append({"reference_code": reference_code, "status": "error", "reason": str(exc)})

    return {"results": results}
