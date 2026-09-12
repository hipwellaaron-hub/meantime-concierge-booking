"""Session/CSRF plumbing for the staff dashboard. This is the first
cookie-authenticated surface in the app -- every other write route
(documents, invoices, the wizard) is gated by an unguessable token in the
URL instead, which is safe from CSRF by construction (no ambient
authority, nothing to forge). A cookie-authenticated route doesn't have
that property, so admin POST routes need an explicit CSRF check that the
rest of the app has never needed.
"""

import secrets
import uuid

from fastapi import Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.staff_user import StaffUser
from app.services import stripe_integration


class NotAuthenticated(Exception):
    """Raised by require_staff; caught by a handler in app.main that
    redirects to /admin/login instead of returning a bare 401 -- this is
    a browser-driven dashboard, not an API client."""


def require_staff(request: Request, db: Session = Depends(get_db)) -> StaffUser:
    staff_id = request.session.get("staff_id")
    if not staff_id:
        raise NotAuthenticated()
    staff = db.get(StaffUser, uuid.UUID(staff_id))
    if staff is None or not staff.is_active:
        request.session.clear()
        raise NotAuthenticated()
    if staff.role == "floor":
        # Floor accounts exist for the read-only Meantime Floor app ONLY.
        # A floor login must never open the admin -- cleared and bounced,
        # exactly as if never authenticated.
        request.session.clear()
        raise NotAuthenticated()
    return staff


def current_venue(request: Request, db: Session = Depends(get_db)):
    """The venue every page under /admin is about, stashed on request.state
    so admin_ctx can read it without 17 call sites passing it by hand.

    A ROUTER-level dependency rather than a per-route argument: a new route
    added to an admin router gets it without anyone remembering, which is the
    property that matters -- app/api/admin_drafts.py contains no mention of a
    venue at all today, and that is exactly how a page ends up outside the
    scoping.

    STILL HARDCODED. This is the seam, not the switch: when the venue switch
    lands, only this function changes and every admin page follows. Keeping
    the hardcode in ONE function rather than eight is the point of doing it
    before the switch rather than during it.
    """
    from app.models import Venue

    venue = db.query(Venue).filter_by(slug="hamilton").one()
    request.state.venue = venue
    return venue


def start_session(request: Request, staff: StaffUser) -> None:
    """Full session reset on login -- not just adding a key -- to avoid
    session fixation (a pre-login csrf_token or any other stale session
    state must not survive into an authenticated session)."""
    request.session.clear()
    request.session["staff_id"] = str(staff.id)
    request.session["csrf_token"] = secrets.token_urlsafe(32)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    session_token = request.session.get("csrf_token")
    if not session_token or not secrets.compare_digest(session_token, csrf_token):
        raise HTTPException(status_code=403, detail="Your session expired -- please refresh and try again")


def admin_ctx(request: Request, staff: StaffUser | None = None, **extra) -> dict:
    ctx = {
        "request": request,
        "staff": staff,
        "csrf_token": ensure_csrf_token(request),
        # Which venue this page is about, put on request.state by the
        # router-level `current_venue` dependency above. A caller may still
        # override it by passing `venue=...`, which `extra` applies below.
        #
        # Every admin page shows it, not just the ones where it is
        # ambiguous. The failure a venue switch actually has is not a wrong
        # query -- the predicates and the composite FK handle that -- it is a
        # correct page read as the other one. A band that appears only
        # sometimes is one nobody learns to read, so it is here rather than
        # on the pages that happen to feel risky.
        "venue": getattr(request.state, "venue", None),
        # Every admin page gets this automatically, not just the ones that
        # touch payments -- the risk this guards against ("staff assumes
        # real money is moving") isn't confined to the invoice screen.
        "stripe_mode": stripe_integration.get_mode(),
    }
    ctx.update(extra)
    return ctx
