"""Venue set-up: the page that creates and fills in a company.

THERE WAS NO PATH AT ALL. `app/seed.py` creates Hamilton and only Hamilton,
nothing else in the application has ever created a Venue row, and there is
no Railway exec or CLI from the build machine. So the second company --
Nice Try Events Pty Ltd, trading as Meantime The Entrance, launching in
two weeks -- could not be brought into existence except by typing SQL into
a database console, one statement, fourteen columns, no validation, on the
row whose contents print on every invoice and every contract that company
will ever issue.

The readiness check added the same day says what is missing. This is where
it gets filled in, which is what makes that check actionable rather than a
dead end.

WHAT THIS PAGE DELIBERATELY REFUSES:

  * THE SLUG, after creation. It is in the admin URL of every page about
    this venue, in the Stripe endpoint path its account posts to, and in
    AI_VENUE_SLUG. Changing it silently breaks all three.
  * THE REFERENCE PREFIX, once anything has used it. Every booking
    reference and every invoice reference a client holds is built from it
    and frozen at issue; the invoice trigger refuses to rewrite one. So a
    venue mid-life would carry two prefixes with no record of when it
    changed. Editable while the venue has issued neither, which is the
    window this page exists for.

WHAT IT DELIBERATELY DOES: creating a venue also creates its "Unassigned
(pending triage)" space. That is not a business decision anybody should be
asked to make -- it is the space every public enquiry is filed against, and
a venue without one serves its enquiry form as a 200 and 500s on the
submit, losing the lead with no booking and no notification (proven
2026-09-14). A bookable room IS a business decision and is not invented
here; the readiness check reports its absence.

THE PAGE IS ABOUT EVERY VENUE while sitting under one venue's URL, and
that is the exception rather than the rule here. It is the same shape as
the AI gates: one company's operator setting up another company. The band
still names the venue whose page you are on, and each row names the venue
it is about, so nothing is ambiguous about which company a field belongs
to.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Booking, Space, StaffUser, Venue
from app.seed import CLIENT_FACING_COLUMNS, UNASSIGNED_SPACE_NAME
from app.services import venue_readiness
from app.templating import templates
from app.venue_scope import venue_scope

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/{venue_slug}/venues",
    tags=["admin-venues"],
    dependencies=[Depends(require_staff), Depends(venue_scope)],
)

# The text columns this form writes. reference_prefix and trading_days are
# handled separately -- one is conditionally locked, the other is a set of
# checkboxes -- and slug and name are set once, at creation.
EDITABLE_TEXT_COLUMNS = (
    "trading_name", "legal_name", "abn", "address", "phone",
    "contact_name", "contact_email",
    "bank_account_name", "bank_bsb", "bank_account_number",
    "licence_number", "licensed_manager",
    "stripe_secret_key_env", "stripe_webhook_secret_env", "stripe_account_id",
    "digest_recipient_email",
)

WEEKDAYS = (
    (0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"),
    (4, "Fri"), (5, "Sat"), (6, "Sun"),
)


def _redirect(request: Request, outcome: str | None = None) -> RedirectResponse:
    url = f"{request.state.venue_base}/venues"
    if outcome:
        url = f"{url}?outcome={outcome}"
    return RedirectResponse(url=url, status_code=303)


def _clean(value: str | None) -> str | None:
    """Blank and whitespace both mean "not given".

    Empty string rather than NULL is what an HTML form posts for a field
    nobody filled in, and the readiness check, the reference generator and
    the webhook resolver all treat whitespace as missing -- so storing ""
    would put the row in a state where it reads unfilled everywhere and
    looks filled in the table.
    """
    cleaned = (value or "").strip()
    return cleaned or None


def _prefix_is_locked(db: Session, venue: Venue) -> bool:
    """Has anything a client holds been built from this prefix yet?

    BOOKINGS ONLY, and that is a deliberate narrowing rather than an
    oversight. The first draft also counted invoices, on the reasoning that
    the two number independently -- a venue can take an enquiry
    (HAM-20261107-QS8U8) without ever issuing an invoice. True, but not the
    other way round: every invoice belongs to a booking, the composite FK
    ties it to that booking's venue, archiving only changes a booking's
    status, and delete_booking_and_dependents removes the invoices with it.
    So an invoice at this venue implies a booking at this venue, and the
    second count could never be the one that answered.

    Proven, not reasoned: a mutation deleting the booking half was caught by
    test; the one deleting the invoice half changed nothing. A guard with
    nothing behind it reads as two protections where there is one.

    On Booking.venue_id rather than a join through Space, because that
    column IS the booking's venue and is immutable by database trigger.
    """
    return bool(
        db.scalar(
            select(func.count()).select_from(Booking).where(Booking.venue_id == venue.id)
        )
    )


@router.get("", response_class=HTMLResponse)
def venues_list(
    request: Request,
    outcome: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    venues = db.scalars(select(Venue).order_by(Venue.name)).all()
    rows = [
        {
            "venue": v,
            "readiness": venue_readiness.check(db, v),
            "prefix_locked": _prefix_is_locked(db, v),
            "trading_days": set(v.trading_days or []),
        }
        for v in venues
    ]
    return templates.TemplateResponse(
        request,
        "admin/venues.html",
        admin_ctx(
            request, staff,
            rows=rows,
            outcome=outcome,
            weekdays=WEEKDAYS,
            editable_columns=EDITABLE_TEXT_COLUMNS,
            client_facing_columns=set(CLIENT_FACING_COLUMNS),
            blocking=venue_readiness.BLOCKING_CONSEQUENCE,
        ),
    )


@router.post("", dependencies=[Depends(require_csrf)])
def create_venue(
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """A venue and its triage space, and nothing else.

    Deliberately only the two fields that can never be filled in later:
    the slug is immutable once anything links to it, and the name is the
    label the rest of the page is organised by. Everything a CLIENT reads
    is entered afterwards, against a row that already exists and whose
    gaps the readiness check is already naming.
    """
    name = (name or "").strip()
    slug = (slug or "").strip().lower()
    if not name or not slug:
        raise HTTPException(status_code=422, detail="A venue needs a name and a slug")
    if not slug.replace("-", "").isalnum():
        # It becomes a URL segment and a Stripe endpoint path.
        raise HTTPException(
            status_code=422,
            detail="A slug may contain only letters, numbers and hyphens",
        )

    venue = Venue(name=name, slug=slug)
    db.add(venue)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=f"A venue with slug {slug!r} already exists") from exc

    # The triage space, always. See the module docstring: without it the
    # venue's public enquiry form renders and then 500s on submit.
    db.add(Space(
        venue_id=venue.id, name=UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=0, is_bookable=False,
    ))
    db.commit()
    logger.warning(
        "Venue %r (%s) created by %s -- a new company now exists in this database",
        name, slug, getattr(staff, "email", "?"),
    )
    return _redirect(request, "created")


@router.post("/{venue_id}", dependencies=[Depends(require_csrf)])
async def update_venue(
    venue_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """The client-facing columns, read straight off the form.

    Read from the raw form rather than declared parameters so one list --
    EDITABLE_TEXT_COLUMNS -- decides what this writes. A declared-parameter
    version means a column added to the model is silently uneditable until
    somebody remembers this function, which is the same shape as the eight
    `_venue()` helpers that all had to be remembered separately.
    """
    # BY ID, NOT SCOPED TO THE PAGE'S VENUE, and that is deliberate -- see
    # the module docstring. This one page is about every venue, because the
    # venue being set up is by definition not one you can already be "in":
    # it may have no slug you could navigate to and no data to show. Every
    # OTHER by-id admin lookup checks request.state.venue and must keep
    # doing so; do not "fix" this one to match them.
    venue = db.get(Venue, venue_id)
    if venue is None:
        raise HTTPException(status_code=404, detail="No such venue")

    form = await request.form()
    for column in EDITABLE_TEXT_COLUMNS:
        if column in form:
            setattr(venue, column, _clean(form.get(column)))

    if "reference_prefix" in form:
        prefix = (_clean(form.get("reference_prefix")) or "").upper() or None
        if prefix != venue.reference_prefix:
            if _prefix_is_locked(db, venue):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "This venue has already issued a booking or invoice reference, so its "
                        "prefix cannot change -- every reference a client holds was built from "
                        "it and is frozen."
                    ),
                )
            if prefix and len(prefix) > 5:
                # reference_code is String(20) and carries prefix + date + suffix.
                raise HTTPException(status_code=422, detail="A reference prefix is at most 5 characters")
            venue.reference_prefix = prefix

    # Trading days: OPEN days, and an empty set is a real answer ("open no
    # days") distinct from NULL ("nobody has said"). The form always posts
    # the marker, so a venue whose every box is unticked records [] rather
    # than falling back to NULL and inheriting nothing.
    if "trading_days_present" in form:
        venue.trading_days = sorted(
            int(d) for d, _ in WEEKDAYS if f"day_{d}" in form
        )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="Another venue already uses that reference prefix",
        ) from exc

    logger.warning(
        "Venue %s identity updated by %s", venue.slug, getattr(staff, "email", "?"),
    )
    return _redirect(request, "saved")
