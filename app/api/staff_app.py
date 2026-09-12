"""The Meantime Floor app's read-only API. Everything here answers one
operational question for the floor and bar team -- what's on, when, and
has it paid -- from data Concierge already holds. Nothing is created or
edited through these routes, ever.

Auth is a DB-backed bearer token (see app.models.staff_app_token),
revocable per-device from the admin staff page. Both roles may use the
app; only role='floor' is BLOCKED from /admin (see app.admin_auth).
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Booking, Document, Invoice, Space, Venue
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.models.staff_user import StaffUser
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.services import beo_rules
from app.services import documents as documents_service
from app.services import staff_auth
from app.services.document_generation import format_date_long
from app.services.pdf import render_html_to_pdf
from app.templating import templates, venue_identity

router = APIRouter(prefix="/api/staff", tags=["staff-app"])

# Same shape as the admin login limiter: brute-force protection on a
# credentialed endpoint, generous enough for a small team's real logins.
_app_login_rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=300)

# The two "locked" booking states the app shows -- per Aaron's explicit
# decision, tentative holds are NOT shown (a hold on Saturday reads as a
# free night to the floor team, and that is the chosen trade-off).
FLOOR_VISIBLE_STATUSES = (BookingStatus.confirmed, BookingStatus.completed)


class StaffLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=255)


def require_app_token(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> StaffUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    staff = staff_auth.get_staff_by_app_token(db, authorization.removeprefix("Bearer ").strip())
    if staff is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return staff


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


@router.post("/login", dependencies=[Depends(rate_limit_dependency(_app_login_rate_limiter))])
def app_login(payload: StaffLogin, request: Request, db: Session = Depends(get_db)):
    staff = staff_auth.authenticate(db, payload.email, payload.password)
    if staff is None:
        raise HTTPException(status_code=401, detail="Wrong email or password")
    token = staff_auth.issue_app_token(db, staff)
    return {"token": token, "name": staff.name}


def _payment_status(db: Session, booking: Booking) -> str:
    """"paid" only when at least one invoice is paid AND nothing
    non-cancelled is still unpaid (draft or sent). A confirmed function
    with no invoices at all is "outstanding" -- the weekend question is
    "did they pay?", and the honest answer there is no. Status only:
    no amounts ever cross this API."""
    invoices = db.scalars(
        select(Invoice).where(Invoice.booking_id == booking.id, Invoice.status != InvoiceStatus.cancelled)
    ).all()
    has_paid = any(i.status == InvoiceStatus.paid for i in invoices)
    has_unpaid = any(i.status in (InvoiceStatus.draft, InvoiceStatus.sent) for i in invoices)
    return "paid" if has_paid and not has_unpaid else "outstanding"


def _floor_beo(db: Session, booking: Booking):
    """(the version the floor works from, the newer version it must not),
    as narrow (id, version, status, is_current, is_legacy) rows -- one
    query per booking, no document content (see documents.version_rows).

    Approved beats current (Aaron, 2026-09-10): the floor works from what
    the client actually agreed to. If staff have revised it since, that
    revision -- draft, sent or viewed -- is something the client has not
    approved and may never have seen, so the team gets the approved one
    and is told a newer version exists.

    With no approval anywhere: the current version if it has gone out;
    otherwise -- a Revise in flight (the draft is current; the client's
    link already answers "being updated"), or a draft deleted so that no
    version is current -- the last version that went out, with a note.
    Aaron: "a blank screen mid-service is worse than a stale one with a
    warning on it."

    A legacy row is never the working version: its content is a
    placeholder for an uploaded PDF, and the floor was the one surface
    still rendering that placeholder. No writer in the repo creates a
    legacy Event Order, so this is the defensive path, stated rather than
    assumed."""
    rows = documents_service.version_rows(db, booking.id, DocumentType.beo)
    approved = next((r for r in rows if r.status == DocumentStatus.signed and not r.is_legacy), None)
    current = next((r for r in rows if r.is_current), None)
    if approved is not None:
        newer = current if current is not None and current.id != approved.id else None
        return approved, newer
    if current is not None and not current.is_legacy and current.status != DocumentStatus.draft:
        return current, None
    last_sent = next(
        (r for r in rows if r.status in (DocumentStatus.sent, DocumentStatus.viewed) and not r.is_legacy), None
    )
    if last_sent is not None:
        newer = current if current is not None and not current.is_legacy and current.id != last_sent.id else None
        return last_sent, newer
    return None, None


def _floor_rsa_gap(document: Document, booking: Booking) -> str | None:
    """The one warning that is not a change: RSA applies to this booking
    now (an 18th, or under-18s on it) and the version on screen never
    carried the RSA line in its Special notes. Kept apart from the "what
    changed" list (review, 2026-09-10) because it is true of an untouched
    approval too, and a heading that says "changed" must not lie."""
    rsa_now = booking.child_count > 0 or beo_rules.looks_like_eighteenth(
        event_type=booking.event_type, event_name=booking.event_name, notes=booking.notes
    )
    if rsa_now and not beo_rules.mentions_rsa((document.content or {}).get("special_notes")):
        return (
            "RSA applies to this booking (an 18th, or under-18s on it) and this version's "
            "Special notes do not carry the RSA line."
        )
    return None


def _floor_drift(document: Document, booking: Booking) -> list[str]:
    """Facts on the booking that differ from the snapshot this version was
    built from. The floor works from an APPROVED version, which can be
    older than the booking: a date, room or guest-count change made after
    approval is invisible on the run sheet itself (the header band reads
    the live name and rooms, the rest is the snapshot), so the floor note
    says what moved. Under-18s FIRST (Aaron, 2026-09-10: "the floor needs
    it more than they need a room change"). Only real deltas belong here;
    the standing RSA warning is _floor_rsa_gap."""
    content = document.content or {}
    ref = content.get("_reference") or {}
    timeline = content.get("event_timeline") or {}
    drift: list[str] = []
    then_kids = ref.get("child_count")
    if then_kids is not None and then_kids != booking.child_count:
        drift.append(f"under-18s are now {booking.child_count} (this version says {then_kids})")
    live_date = format_date_long(booking.event_date) if booking.event_date else None
    then_date = timeline.get("event_date_display")
    if live_date and then_date and then_date != live_date:
        drift.append(f"the date is now {live_date} (this version was built for {then_date})")
    then_rooms = ref.get("space_name")
    if then_rooms and then_rooms != booking.all_space_names:
        drift.append(f"the rooms are now {booking.all_space_names} (this version says {then_rooms})")
    then_adults = ref.get("adult_count")
    if then_adults is not None and then_adults != booking.adult_count:
        drift.append(f"adults are now {booking.adult_count} (this version says {then_adults})")
    then_name = ref.get("event_name")
    if then_name and then_name != booking.event_name:
        drift.append(f"the event is now named {booking.event_name!r} (this version says {then_name!r})")
    return drift


def _booking_payload(db: Session, booking: Booking) -> dict:
    spaces = [booking.space.name]
    for child in booking.linked_bookings:
        if child.status in FLOOR_VISIBLE_STATUSES:
            spaces.append(child.space.name)
    working, newer = _floor_beo(db, booking)
    return {
        "id": str(booking.id),
        "date": booking.event_date.isoformat() if booking.event_date else None,
        "space": booking.space.name,
        "spaces": spaces,
        "event_name": booking.event_name,
        "event_type": booking.event_type,
        "start_time": booking.start_time.strftime("%H:%M") if booking.start_time else None,
        "end_time": booking.end_time.strftime("%H:%M") if booking.end_time else None,
        "adults": booking.adult_count,
        "kids": booking.child_count,
        "status": booking.status.value,
        "beo_ready": working is not None,
        # Whether the version the floor will open is the client's approved
        # one, and -- if staff have since revised it -- the version they
        # must NOT work from, named so the screen can say so.
        "beo_approved": working is not None and working.status == DocumentStatus.signed,
        "beo_newer_unapproved": {"version": newer.version, "status": newer.status.value} if newer is not None else None,
        "payment_status": _payment_status(db, booking),
    }


@router.get("/bookings")
def list_bookings(
    request: Request,
    from_date: dt.date | None = Query(default=None, alias="from"),
    to_date: dt.date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    venue = _venue(db)
    query = (
        select(Booking)
        .join(Space, Booking.space_id == Space.id)
        .where(
            Space.venue_id == venue.id,
            Booking.status.in_(FLOOR_VISIBLE_STATUSES),
            # Linked children are their parent's second room, not a
            # separate function -- the parent's payload carries both
            # spaces instead.
            Booking.parent_booking_id.is_(None),
            Booking.event_date.isnot(None),
        )
        .order_by(Booking.event_date, Booking.start_time)
    )
    if from_date is not None:
        query = query.where(Booking.event_date >= from_date)
    if to_date is not None:
        query = query.where(Booking.event_date <= to_date)
    return {"bookings": [_booking_payload(db, b) for b in db.scalars(query).all()]}


def _get_visible_booking_or_404(db: Session, booking_id: uuid.UUID) -> Booking:
    """The floor app's by-id routes -- detail, Event Order, PDF -- all come
    through here.

    The LIST above already filters on Space.venue_id; these did not, so the
    floor's own rule ("a phone opened at one venue shows that venue") held
    for what the app displays and not for what it would fetch by id. Same
    shape as the admin router's helper, same reasoning, and identical
    behaviour while Hamilton is the only venue.

    This matters more here than in admin because the floor app is the one
    surface a second venue's staff would hold: Karly at Hamilton, Ruby at
    The Entrance, each with their own device token. Scoping is a column on
    the staff user plus this predicate, not a login system.
    """
    booking = db.get(Booking, booking_id)
    if (
        booking is None
        or booking.status not in FLOOR_VISIBLE_STATUSES
        or booking.parent_booking_id is not None
        or booking.venue_id != _venue(db).id
    ):
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.get("/bookings/{booking_id}")
def booking_detail(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    booking = _get_visible_booking_or_404(db, booking_id)
    payload = _booking_payload(db, booking)
    # The floor team's run-order facts. Read-only; no client contact
    # details, no dollar figures.
    payload["setup_access_time"] = booking.setup_access_time.strftime("%H:%M") if booking.setup_access_time else None
    payload["setup_access_confirmed"] = booking.setup_access_confirmed
    payload["food_service_time"] = booking.food_service_time.strftime("%H:%M") if booking.food_service_time else None
    payload["guest_arrival_time"] = booking.guest_arrival_time.strftime("%H:%M") if booking.guest_arrival_time else None
    return payload


def _get_floor_beo_or_404(db: Session, booking: Booking) -> tuple[Document, object | None]:
    """The full Document to render, and the narrow row of the newer
    unapproved version if there is one."""
    working, newer = _floor_beo(db, booking)
    if working is None:
        raise HTTPException(status_code=404, detail="No finalised BEO for this booking")
    document = db.get(Document, working.id)
    if document is None:  # pragma: no cover -- the row was read a moment ago
        raise HTTPException(status_code=404, detail="No finalised BEO for this booking")
    return document, newer


@router.get("/bookings/{booking_id}/beo", response_class=HTMLResponse)
def booking_beo(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    """The finalised BEO, rendered for the floor team. Deliberately NOT
    the public /d/{token} link: the first open of that link marks the
    document viewed, and "viewed" must keep meaning THE CLIENT saw it.
    is_floor_app shows the internal kitchen/bar notes -- this is exactly
    the staff surface they exist for."""
    booking = _get_visible_booking_or_404(db, booking_id)
    document, newer = _get_floor_beo_or_404(db, booking)
    return templates.TemplateResponse(
        request,
        "document.html",
        {
            "document": document,
            "booking": booking,
            "is_floor_app": True,
            "floor_newer_version": newer,
            "floor_drift": _floor_drift(document, booking),
            "floor_rsa_gap": _floor_rsa_gap(document, booking),
            **venue_identity(booking.venue),
        },
    )


@router.get("/bookings/{booking_id}/beo.pdf")
def booking_beo_pdf(
    booking_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_app_token),
):
    """Download/share copy: the client render without internal notes (a
    shared PDF can leave the team) and, like the floor screen, without
    the client's phone or the billing summary (Aaron, 2026-09-10: "the
    floor team doesn't need the client's phone on a document that gets
    left on a bar, and the billing summary is a conversation for me")."""
    booking = _get_visible_booking_or_404(db, booking_id)
    document, _newer = _get_floor_beo_or_404(db, booking)
    html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True, floor_pdf=True,
        **venue_identity(booking.venue),
    )
    filename = f"{booking.reference_code}-BEO-v{document.version}.pdf"
    return Response(
        content=render_html_to_pdf(html),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
