import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.venue_scope import venue_scope
from app.models import Venue
from app.models.staff_user import StaffUser
from app.services.attribution import CONFIRMED_STATUSES, current_quarter_start, get_channel_breakdown
from app.templating import templates

router = APIRouter(
    prefix="/admin/{venue_slug}/reports", tags=["admin-reports"],
    dependencies=[Depends(require_staff), Depends(venue_scope)],
)


def _venue(request: Request) -> Venue:
    """The venue named in the URL, resolved by the router-level venue_scope
    dependency and stashed on request.state.

    This used to be `db.query(Venue).filter_by(slug="hamilton").one()`. Once
    the router moved onto /admin/{venue_slug}/, that made the page claim one
    venue in its URL and in its band while querying another -- which looks
    exactly like a correct page, and is worse than a visibly mixed list.

    (app/api/availability.py still carries `venue_slug: str = "hamilton"` as
    a PUBLIC query-parameter default -- a separate surface, not fixed here.)
    """
    return request.state.venue


@router.get("/attribution", response_class=HTMLResponse)
def attribution_report(
    request: Request,
    since: dt.date | None = None,
    until: dt.date | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venue = _venue(request)
    today = dt.date.today()
    range_since = since or current_quarter_start(today)
    # Exclusive upper bound, one day past "until" so the given end date's
    # own bookings are included -- matches how every other date-range
    # query in this app treats an inclusive end date.
    range_until = (until or today) + dt.timedelta(days=1)

    all_breakdown = get_channel_breakdown(db, venue.id, since=range_since, until=range_until, touch="first")
    confirmed_breakdown = get_channel_breakdown(
        db, venue.id, since=range_since, until=range_until, statuses=CONFIRMED_STATUSES, touch="first"
    )

    return templates.TemplateResponse(
        request,
        "admin/reports_attribution.html",
        admin_ctx(
            request,
            staff,
            range_since=range_since,
            range_until=until or today,
            all_breakdown=all_breakdown,
            confirmed_breakdown=confirmed_breakdown,
            all_total=sum(all_breakdown.values()),
            confirmed_total=sum(confirmed_breakdown.values()),
        ),
    )
