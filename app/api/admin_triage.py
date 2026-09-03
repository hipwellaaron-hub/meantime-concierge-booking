from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Space, Venue
from app.models.staff_user import StaffUser
from app.services import booking as booking_service
from app.services import documents as documents_service
from app.services import enquiry_classification, ivvy_import, reconciliation
from app.services import wizard as wizard_service
from app.templating import templates

router = APIRouter(prefix="/admin/triage", tags=["admin-triage"], dependencies=[Depends(require_staff)])


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("", response_class=HTMLResponse)
def triage(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    venue = _venue(db)
    unassigned = ivvy_import.get_unassigned_bookings(db, venue)
    wizard_ready = wizard_service.get_wizard_eligible_bookings(db, venue)
    needs_clarification = enquiry_classification.get_enquiries_needing_clarification(db, venue)
    flagged_in_progress = enquiry_classification.get_flagged_bookings_in_progress(db, venue)
    unrecorded_min_reductions = booking_service.get_bookings_with_unrecorded_minimum_reduction(db, venue.id)
    notification_failures = enquiry_classification.get_enquiry_notification_failures(db, venue)
    beos_to_review = documents_service.get_beos_awaiting_review(db, venue)
    # The nightly job's open findings. Stored since the job was built, but
    # until now nothing rendered them -- a finding nobody can see is not a
    # finding. NOTES_BEFORE_BEO rows carry the text itself so it can be
    # read here, on a staff-only page, rather than scanned booking by booking.
    findings = reconciliation.open_findings(db, venue)
    notes_review = [f.booking for f in findings if f.check_code == "NOTES_BEFORE_BEO"]
    other_findings = [f for f in findings if f.check_code != "NOTES_BEFORE_BEO"]
    bookable_spaces = db.scalars(
        select(Space).where(Space.venue_id == venue.id, Space.is_bookable.is_(True)).order_by(Space.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/triage.html",
        admin_ctx(
            request,
            staff,
            unassigned=unassigned,
            wizard_ready=wizard_ready,
            needs_clarification=needs_clarification,
            flagged_in_progress=flagged_in_progress,
            unrecorded_min_reductions=unrecorded_min_reductions,
            notification_failures=notification_failures,
            beos_to_review=beos_to_review,
            bookable_spaces=bookable_spaces,
            findings=other_findings,
            notes_review=notes_review,
        ),
    )


@router.post("/reconcile", dependencies=[Depends(require_csrf)])
def run_reconciliation_now(
    request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)
):
    """Run the reconciliation checks on demand. Reads everything and fixes
    nothing (see app.services.reconciliation); the only writes are the
    findings rows themselves, which open, persist and self-clear. Exists so
    the checks can be run the moment they matter rather than waiting for
    tonight."""
    reconciliation.run(db, _venue(db))
    return RedirectResponse(url="/admin/triage", status_code=303)
