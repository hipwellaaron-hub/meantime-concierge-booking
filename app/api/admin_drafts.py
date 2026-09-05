"""Stage 1 shadow-mode review (Phase 2 brief section 9).

Drafts are generated but not surfaced to the workflow. This page is where
a human compares what the AI would have sent with what was actually sent,
in one sitting: the draft, the sent version, and why they differed. The
discard reason is the honest measure of whether this is working.

Nothing here sends anything, and nothing here is reachable without a
staff login.
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Booking
from app.models.enquiry_draft import (
    OUTCOME_DISCARDED,
    OUTCOME_EDITED,
    OUTCOME_SENT_UNCHANGED,
    STATUS_GENERATED,
    EnquiryDraft,
)
from app.models.staff_user import StaffUser
from app.services import ai_access, drafting
from app.templating import templates

router = APIRouter(prefix="/admin/drafts", tags=["admin-drafts"], dependencies=[Depends(require_staff)])


@router.get("", response_class=HTMLResponse)
def review_drafts(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    drafts = db.scalars(
        select(EnquiryDraft)
        .options(selectinload(EnquiryDraft.booking).selectinload(Booking.contact))
        .order_by(EnquiryDraft.created_at.desc())
        .limit(100)
    ).all()
    counts: dict[str, int] = {}
    for d in drafts:
        counts[d.status] = counts.get(d.status, 0) + 1
    outcomes: dict[str, int] = {}
    for d in drafts:
        if d.outcome:
            outcomes[d.outcome] = outcomes.get(d.outcome, 0) + 1
    switches = ai_access.get_settings_row(db)
    # Re-verify on surface (Phase 2 brief): a generated draft still awaiting
    # review is checked against a live availability read as the page
    # renders, so a stale draft is flagged before anyone acts on it.
    occupants_cache: dict = {}
    freshness = {
        d.id: drafting.freshness(db, d, cache=occupants_cache)
        for d in drafts
        if d.status == STATUS_GENERATED and d.outcome is None
    }
    return templates.TemplateResponse(
        request, "admin/drafts.html",
        admin_ctx(request, staff, drafts=drafts, counts=counts, outcomes=outcomes, freshness=freshness,
                  drafting_enabled=switches.drafting_enabled, drafts_visible=switches.drafts_visible),
    )


@router.post("/{draft_id}/review", dependencies=[Depends(require_csrf)])
def record_review(
    draft_id: uuid.UUID,
    request: Request,
    outcome: str = Form(...),
    sent_version: str = Form(""),
    edit_reason: str = Form(""),
    discard_reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    if outcome not in (OUTCOME_SENT_UNCHANGED, OUTCOME_EDITED, OUTCOME_DISCARDED):
        raise HTTPException(status_code=422, detail="Unknown outcome")
    draft = db.get(EnquiryDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if outcome == OUTCOME_DISCARDED and not discard_reason.strip():
        raise HTTPException(status_code=422, detail="Say why it was discarded -- that is the signal")

    draft.outcome = outcome
    draft.sent_version = sent_version.strip() or None
    draft.edit_reason = edit_reason.strip() or None
    draft.discard_reason = discard_reason.strip() or None
    draft.reviewed_at = dt.datetime.now(dt.timezone.utc)
    draft.reviewed_by = f"staff:{staff.email}"
    db.commit()
    return RedirectResponse(url="/admin/drafts", status_code=303)


@router.post("/switches", dependencies=[Depends(require_csrf)])
def set_switches(
    request: Request,
    drafting_enabled: str = Form(""),
    drafts_visible: str = Form(""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The Phase 2 switches. Both default off; Stage 1 is drafting on with
    visibility off. A single form so the two cannot be confused for each
    other."""
    row = ai_access.get_settings_row(db)
    row.drafting_enabled = drafting_enabled == "on"
    row.drafts_visible = drafts_visible == "on"
    row.updated_by = f"staff:{staff.email}"
    db.commit()
    return RedirectResponse(url="/admin/drafts", status_code=303)
