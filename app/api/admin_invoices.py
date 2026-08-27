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
from app.models import Venue
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.services import invoicing
from app.templating import templates

router = APIRouter(prefix="/admin/invoices", tags=["admin-invoices"], dependencies=[Depends(require_staff)])


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.get("", response_class=HTMLResponse)
def list_invoices(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # Same empty-string handling as the bookings list: the "Any" option in
    # the filter submits status="", which must mean "no filter" rather
    # than raising on an unknown status.
    try:
        parsed_status = InvoiceStatus(status) if status else None
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")

    venue = _venue(db)
    invoices = invoicing.search_invoices(db, venue.id, status=parsed_status)
    return templates.TemplateResponse(
        request,
        "admin/invoices_list.html",
        admin_ctx(
            request,
            staff,
            invoices=invoices,
            status=parsed_status,
            statuses=list(InvoiceStatus),
            total_outstanding=sum(
                (i.total for i in invoices if i.status == InvoiceStatus.sent), start=0
            ),
        ),
    )
