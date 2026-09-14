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


# `current_venue` LIVED HERE and is deleted (2026-09-14). It was the seam
# for the venue switch -- one hardcoded `filter_by(slug="hamilton").one()`
# in a router-level dependency, so the switch would change one function
# instead of eight. The switch LANDED: all eight admin routers are on
# /admin/{venue_slug}/ and take `venue_scope`, which resolves the venue
# from the path and sets the same request.state.
#
# So it had no callers, and its docstring still said "STILL HARDCODED,
# this is the seam" -- which is the trap the module-constant sweep found
# earlier the same day. A new admin router declaring it would have passed
# the structural test that exists to catch an unscoped router (that test
# accepted EITHER mechanism during the rollout) and served Hamilton's
# bookings under /admin/entrance/. Nothing would have failed, because
# Hamilton is a real venue and its rows are real rows.
#
# tests/test_admin_shows_its_venue.py now requires venue_scope by name.


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
        # router-level `venue_scope` dependency, which reads it from the
        # path segment. A caller may still override it by passing
        # `venue=...`, which `extra` applies below.
        #
        # Every admin page shows it, not just the ones where it is
        # ambiguous. The failure a venue switch actually has is not a wrong
        # query -- the predicates and the composite FK handle that -- it is a
        # correct page read as the other one. A band that appears only
        # sometimes is one nobody learns to read, so it is here rather than
        # on the pages that happen to feel risky.
        "venue": getattr(request.state, "venue", None),
        # The URL prefix a template builds its links from. Set by
        # venue_scope for a scoped page; "/admin" elsewhere, which the
        # compat routes redirect from -- so a template written as
        # `{{ venue_base }}/bookings` is correct on both, and the routers
        # can move one at a time instead of all at once.
        "venue_base": getattr(request.state, "venue_base", "/admin"),
        # Every admin page gets this automatically, not just the ones that
        # touch payments -- the risk this guards against ("staff assumes
        # real money is moving") isn't confined to the invoice screen.
        #
        # THIS VENUE's key, not the process's. Two companies means two keys
        # and one of them can be live while the other is not; a
        # process-wide answer on a venue-scoped page is how somebody takes
        # a deposit through a sandbox key and waits for money that is never
        # coming. Falls back to the process key only where there is no
        # venue in scope at all (the chooser, the login).
        "stripe_mode": stripe_integration.mode_for(getattr(request.state, "venue", None)),
    }
    ctx.update(extra)
    return ctx
