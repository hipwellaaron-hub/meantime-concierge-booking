"""The old /admin URLs, kept working.

Every admin URL grew a venue segment. That would break every bookmark, every
link in a nine-month-old email, and every tab somebody left open -- so the
old shapes stay, and redirect.

TWO KINDS, and the difference matters.

A BY-ID URL knows its own answer. `/admin/bookings/{id}` names a booking, the
booking row names its venue, so the redirect goes to exactly the right page
with no guessing. That is why a stale link to a specific booking still works
perfectly, which is the case that actually happens.

A LIST URL does not. `/admin/bookings` names no venue and nothing in the
request can supply one, so it goes to the chooser -- carrying where it was
trying to go, so picking a venue lands on the bookings list rather than the
dashboard. With exactly one venue the chooser redirects straight through and
nobody ever sees it, which is why this is invisible today.

DELIBERATELY NOT PERMANENT REDIRECTS. 303, not 301: a browser caches a 301
forever, and these routes are meant to be removed once nothing uses them.
A cached 301 would outlive the route and keep working after the redirect
target had changed, which is the worst of both.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.models import Booking
from app.models.staff_user import StaffUser
from app.templating import templates
from app.venue_scope import staff_may_use, venues_for

router = APIRouter(prefix="/admin", tags=["admin-compat"], dependencies=[Depends(require_staff)])


def _chooser_or_redirect(
    request: Request, db: Session, staff: StaffUser, *, then: str = ""
):
    """Send the operator to a venue, asking only when there is a choice.

    `then` is the path WITHIN a venue to land on, e.g. "/bookings". It is
    built from a fixed set of literals at the call sites below and never from
    anything the caller supplies, so it cannot become an open redirect.
    """
    usable = venues_for(db, staff)

    if not usable:
        # Not a 404: the operator is signed in and there is genuinely nowhere
        # to send them, which is a configuration fault worth naming.
        return templates.TemplateResponse(
            request, "admin/no_venue.html", admin_ctx(request, staff), status_code=503,
        )

    if len(usable) == 1:
        return RedirectResponse(url=f"/admin/{usable[0].slug}{then}", status_code=303)

    return templates.TemplateResponse(
        request, "admin/choose_venue.html",
        admin_ctx(request, staff, venues=usable, then=then),
    )


@router.get("/", response_class=HTMLResponse)
def admin_root(request: Request, db: Session = Depends(get_db), staff: StaffUser = Depends(require_staff)):
    """The front door. One venue redirects straight to its dashboard."""
    return _chooser_or_redirect(request, db, staff)


# ONLY the routers that have already MOVED onto /admin/{venue_slug}/.
#
# This list is not "the legacy paths" -- it is "the legacy paths whose real
# route is gone". A compat entry for a router that has NOT moved yet is
# registered ahead of that router's own route and SHADOWS it, redirecting to
# a venue-scoped URL that does not exist. It fails as a 404 on a page that
# worked a minute ago, which is how an incremental rollout turns into an
# outage. Caught by test_no_compat_route_shadows_a_live_one.
#
# Add an entry here in the SAME commit that moves its router, never before.
MOVED_LIST_PATHS: tuple[tuple[str, str], ...] = (
    ("reports/attribution", "/reports/attribution"),
    ("calendar", "/calendar"),
    ("drafts", "/drafts"),
    ("invoices", "/invoices"),
    ("staff", "/staff"),
    ("triage", "/triage"),
    ("bookings", "/bookings"),
)

for _legacy, _then in MOVED_LIST_PATHS:

    def _make(then: str):
        def legacy_list(
            request: Request,
            db: Session = Depends(get_db),
            staff: StaffUser = Depends(require_staff),
        ):
            return _chooser_or_redirect(request, db, staff, then=then)

        return legacy_list

    router.add_api_route(
        f"/{_legacy}", _make(_then), methods=["GET"], include_in_schema=False,
        name=f"legacy_{_legacy.replace('/', '_')}",
    )


@router.get("/bookings/{booking_id}", include_in_schema=False)
def legacy_booking(
    booking_id: uuid.UUID,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The one that matters, and the reason the by-id and list cases were
    always going to be different.

    A stale link to a SPECIFIC booking can answer its own question: the
    booking row names its venue. So this resolves the venue from the row and
    redirects to exactly the right page, with no chooser and no guessing.

    THIS IS WHY EMAILS KEEP THE LEGACY PATH. The eight sites in app/services
    that build {dashboard_base_url}/admin/bookings/{id} -- the digest, the
    enquiry alert, the BEO review link -- are deliberately NOT rewritten to
    scoped URLs. A link that works out its own venue cannot go stale, cannot
    be wrong when the message is forwarded, and still resolves a year later
    if the venue is renamed. A baked-in slug fails all three.

    It is also the cross-venue case Aaron asked to fix: an operator in one
    venue who opens a link to the other's booking is TAKEN THERE, with the
    band already showing where they now are. The alternative -- the bare 404
    this used to give -- reads identically to "no such booking".
    """
    booking = db.get(Booking, booking_id)
    if booking is None or not staff_may_use(staff, booking.venue):
        # Identical response for "does not exist" and "not yours", which is
        # the honest answer in both cases to somebody who cannot see it.
        return RedirectResponse(url="/admin/", status_code=303)
    return RedirectResponse(
        url=f"/admin/{booking.venue.slug}/bookings/{booking.id}", status_code=303
    )
