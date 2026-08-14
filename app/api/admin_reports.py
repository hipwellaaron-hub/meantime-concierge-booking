import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.models import Venue
from app.models.staff_user import StaffUser
from app.services.attribution import CONFIRMED_STATUSES, current_quarter_start, get_channel_breakdown
from app.templating import templates

router = APIRouter(prefix="/admin/reports", tags=["admin-reports"], dependencies=[Depends(require_staff)])


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("/attribution", response_class=HTMLResponse)
def attribution_report(
    request: Request,
    since: dt.date | None = None,
    until: dt.date | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venue = _venue(db)
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
