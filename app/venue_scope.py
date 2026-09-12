"""Which venue an admin page is about, taken from the URL.

THE PROBLEM THIS SOLVES is the write path, not the read path. Reads were
already scoped: the by-id helpers carry a venue predicate and the composite
FK makes a booking's venue unwritable if it disagrees with its space's.
`POST /admin/bookings/new` is the one that has no such anchor -- it takes no
space_id and its form carries one hidden field -- so the venue of a newly
created booking is decided entirely by whatever the venue mechanism says at
SUBMIT time.

Under a session-stored venue there is no second witness to that. Open the
new-booking form, switch venue in another tab, submit the first one, and the
booking is created at the other company. `bookings.venue_id` is immutable by
database trigger (d8c3f1a7e920), so the remedy is not an UPDATE -- it is
deleting and recreating the booking after the client already holds their
reference code.

Under a path segment the form posts to `/admin/{venue}/bookings/new`. The
venue it was rendered for is in the action attribute, and no amount of
clicking elsewhere changes it. Two tabs are two venues by construction.

NO DEFAULT ANYWHERE IN THIS FILE. A route registered without the segment
makes FastAPI reclassify `venue_slug` as a required QUERY parameter, so it
returns 422 on every request rather than quietly serving a default venue --
a forgotten segment cannot serve the wrong thing, it cannot serve anything.
And the query-string escape from that is closed below: a value that did not
arrive in the path is refused.
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_auth import require_staff
from app.database import get_db
from app.models import Booking, Venue
from app.models.staff_user import StaffUser


# First path segments already spoken for under /admin. A venue slugged with
# one of these would make `/admin/{venue_slug}/...` ambiguous against the
# legacy route of the same name -- which one wins would depend on router
# registration order, which is not a thing anybody should have to reason
# about to answer "whose booking is this".
#
# Checked when a scope is resolved rather than only when a venue is created,
# because a venue row can be written by a migration, a seed or by hand.
RESERVED_SLUGS = frozenset({
    "bookings", "calendar", "drafts", "invoices", "login", "logout",
    "reports", "staff", "triage",
})


@dataclass(frozen=True)
class VenueScope:
    """The venue, plus the URL prefix every link on the page must start
    with. `base` exists so a template writes `{{ base }}/bookings` rather
    than rebuilding the prefix from the slug in 124 places."""

    venue: Venue
    base: str


def staff_may_use(staff: StaffUser, venue: Venue) -> bool:
    """Whether this person may work at this venue.

    Aaron is the only admin and works both buildings, so today every admin
    may use every venue. It is a NAMED FUNCTION rather than an inline `True`
    so that the day a bookkeeper, a partner or a buyer needs one company's
    records only, there is ONE place to change instead of a search for
    wherever the assumption was made.
    """
    return staff.role == "admin"


def venue_scope(
    request: Request,
    venue_slug: str,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
) -> VenueScope:
    """Resolve the venue named in the path. No default, ever.

    The `request.path_params` check is not belt-and-braces. Without the
    segment on a route, FastAPI treats `venue_slug` as a query parameter,
    and `/admin/bookings?venue_slug=hamilton` would then resolve and serve a
    page -- a URL anybody could construct that reaches a venue by a route
    that was never scoped. Refusing anything that did not arrive in the PATH
    closes that, and turns the mistake into a 422 at the door.
    """
    if request.path_params.get("venue_slug") != venue_slug:
        raise HTTPException(
            status_code=404,
            detail="Not found",
        )

    if venue_slug in RESERVED_SLUGS:
        # Not reachable through a legitimate venue, and reaching it means
        # somebody has created a venue whose slug shadows an admin path.
        raise HTTPException(status_code=404, detail="Not found")

    venue = db.scalars(select(Venue).where(Venue.slug == venue_slug)).one_or_none()
    if venue is None:
        raise HTTPException(status_code=404, detail="No such venue")

    if not staff_may_use(staff, venue):
        # 404 rather than 403: whether a venue exists is not this person's
        # business if they may not work at it.
        raise HTTPException(status_code=404, detail="No such venue")

    scope = VenueScope(venue=venue, base=f"/admin/{venue.slug}")
    # Stashed so admin_ctx can put the venue band on the page without every
    # route passing it by hand.
    request.state.venue = venue
    request.state.venue_base = scope.base
    return scope


def venues_for(db: Session, staff: StaffUser) -> list[Venue]:
    """Every venue this person may work at, in a stable order.

    Ordered by name, not by id or insertion: a chooser whose options move
    between visits is one you have to read rather than point at.
    """
    return [
        v for v in db.scalars(select(Venue).order_by(Venue.name)).all()
        if v.slug not in RESERVED_SLUGS and staff_may_use(staff, v)
    ]


def booking_in_scope(
    booking_id: uuid.UUID,
    scope: VenueScope = Depends(venue_scope),
    db: Session = Depends(get_db),
) -> Booking:
    """A booking, but only this venue's.

    A DEPENDENCY rather than a helper the route calls, so a by-id route
    cannot fetch a booking without the venue check having run -- there is no
    version of the route that compiles and skips it. `db.get(Booking, id)`
    appearing anywhere in an admin router is the shape this exists to
    prevent, and a test greps for exactly that.

    404, not 403: the venue a booking belongs to is not this operator's
    secret, but a different status code would tell a caller the id exists
    somewhere. "Not found here" is the true answer either way, and the
    compat route is where a person who followed a stale link gets sent
    somewhere useful instead.
    """
    booking = db.get(Booking, booking_id)
    if booking is None or booking.venue_id != scope.venue.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking
