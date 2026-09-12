"""Venue-wide invoice list. Individual invoice actions (create, send,
record payment) live on the booking detail page in app.api.admin_bookings
-- an invoice only ever makes sense in the context of its booking. This is
purely the "show me everything outstanding" view the dashboard's unpaid
count links to.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_staff
from app.database import get_db
from app.venue_scope import venue_scope
from app.models import Venue
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.services import invoicing
from app.templating import templates

router = APIRouter(prefix="/admin/{venue_slug}/invoices", tags=["admin-invoices"], dependencies=[Depends(require_staff), Depends(venue_scope)])


def _venue(request: Request) -> Venue:
    """The venue named in the URL, resolved by the router-level venue_scope
    dependency and stashed on request.state.

    This used to be `db.query(Venue).filter_by(slug="hamilton").one()`. Once
    the router moved onto /admin/{venue_slug}/, that made the page claim one
    venue in its URL and in its band while querying another -- which looks
    exactly like a correct page, and is worse than a visibly mixed list.

    (The public availability endpoint had the same shape as a query-parameter
    default; c28d30a made it `Query(..., min_length=1)`, so it refuses rather
    than choosing a building for the caller.)
    """
    return request.state.venue


@router.get("", response_class=HTMLResponse)
def list_invoices(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # Same handling as the bookings list: the default option submits
    # status="" (active only), and status="all" is the explicit
    # show-everything escape hatch; an unknown value 422s.
    include_terminal = status == "all"
    try:
        parsed_status = None if (not status or status == "all") else InvoiceStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")

    venue = _venue(request)
    invoices = invoicing.search_invoices(db, venue.id, status=parsed_status, include_terminal=include_terminal)
    return templates.TemplateResponse(
        request,
        "admin/invoices_list.html",
        admin_ctx(
            request,
            staff,
            invoices=invoices,
            status=parsed_status,
            status_filter=status or "",
            statuses=list(InvoiceStatus),
            total_outstanding=sum(
                (i.total for i in invoices if i.status == InvoiceStatus.sent), start=0
            ),
        ),
    )
