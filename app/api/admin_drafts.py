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

from app.config import settings

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.venue_scope import venue_scope
from app.models import Booking, Venue
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

router = APIRouter(prefix="/admin/{venue_slug}/drafts", tags=["admin-drafts"], dependencies=[Depends(require_staff), Depends(venue_scope)])


def _venue(request: Request) -> Venue:
    """The venue named in the URL, resolved by the router-level venue_scope
    dependency and stashed on request.state."""
    return request.state.venue


@router.get("", response_class=HTMLResponse)
def review_drafts(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    venue = _venue(request)
    drafts = db.scalars(
        select(EnquiryDraft)
        .join(Booking, EnquiryDraft.booking_id == Booking.id)
        # Scoped through the booking's own venue column. Without this the
        # page listed every venue's drafts under one venue's URL and band --
        # a confidently mislabelled list, which is worse than a mixed one
        # because nothing about it looks wrong.
        .where(Booking.venue_id == venue.id)
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
                  drafting_enabled=switches.drafting_enabled, drafts_visible=switches.drafts_visible,
                  # The Phase 1 master gates, which had no UI at all until
                  # 2026-09-14 -- reachable only by a direct database write.
                  access_enabled=switches.access_enabled, writes_enabled=switches.writes_enabled,
                  env_access_enabled=settings.ai_access_enabled,
                  env_writes_enabled=settings.ai_writes_enabled),
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
    # Not found rather than forbidden, and checked against the URL's venue:
    # a draft belonging to another venue is not this page's to mark
    # reviewed, and saying "forbidden" would confirm it exists.
    if draft is None or draft.booking is None or draft.booking.venue_id != _venue(request).id:
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
    return RedirectResponse(url=f"{request.state.venue_base}/drafts", status_code=303)


@router.post("/master-switches", dependencies=[Depends(require_csrf)])
def set_master_switches(
    request: Request,
    access_enabled: str = Form(""),
    writes_enabled: str = Form(""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The Phase 1 master gates, which had NO UI AT ALL until 2026-09-14.

    access_enabled and writes_enabled appeared in no template and no route:
    the only ways to change them were a direct database write or the env
    var override, and /healthz did not report them either. A kill switch
    that can only be reached with psql is not a kill switch -- the whole
    point of putting them in the database rather than in the environment
    was that an env var "needs a ~90s rebuild on Railway, which is not a
    kill switch" (app/services/ai_access.py).

    Turning ACCESS off closes writes too, because writes_enabled is read as
    `row.access_enabled and row.writes_enabled` -- the form says so rather
    than leaving somebody to discover it.

    The env vars remain a hard override in the other direction: either
    source saying false wins, so this cannot turn on something the
    environment has turned off. The page says that too.
    """
    row = ai_access.get_settings_row(db)
    row.access_enabled = access_enabled == "on"
    row.writes_enabled = writes_enabled == "on"
    if not row.access_enabled or not row.writes_enabled:
        # The model already carries these two columns and nothing was
        # writing them. A gate that closes without recording when or why is
        # the same shape as the stamps fixed earlier today.
        row.writes_disabled_at = dt.datetime.now(dt.timezone.utc)
        row.writes_disabled_reason = f"turned off from the AI access page by {staff.email}"
    row.updated_by = f"staff:{staff.email}"
    db.commit()
    return RedirectResponse(url=f"{request.state.venue_base}/drafts", status_code=303)


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
    other.

    PROCESS-WIDE, NOT PER VENUE. AiSettings is a single row with no
    venue_id, so turning drafting off from /admin/entrance/drafts turns it
    off for Hamilton too. That is a column and a decision, not a filter;
    until it exists the template says so above the form, because a
    venue-scoped URL around a global switch is the same trap as a
    venue-scoped URL around a global list."""
    row = ai_access.get_settings_row(db)
    row.drafting_enabled = drafting_enabled == "on"
    row.drafts_visible = drafts_visible == "on"
    row.updated_by = f"staff:{staff.email}"
    db.commit()
    return RedirectResponse(url=f"{request.state.venue_base}/drafts", status_code=303)
