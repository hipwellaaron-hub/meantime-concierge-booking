"""Staff account management for Concierge admin -- the page the Meantime
Floor brief requires: floor accounts are created, disabled and their app
tokens revoked from here, never via a public route. Until now accounts
only existed via the one-off CLI (app.create_staff_user); that still
works and is unchanged.
"""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.venue_scope import venue_scope
from app.models import StaffAppToken, StaffUser
from app.services import notifications, staff_auth
from app.templating import templates

router = APIRouter(prefix="/admin/{venue_slug}/staff", tags=["admin-staff"], dependencies=[Depends(require_staff), Depends(venue_scope)])


def _redirect(request: Request) -> RedirectResponse:
    """Back to this venue's staff page.

    The venue base, never the literal "/admin/staff": that path belongs to
    the compat route now, which rebuilds its destination from a fixed
    literal and so drops any query string -- which silently lost the
    ?outcome= that puts the confirmation banner on the page.
    """
    return RedirectResponse(url=f"{request.state.venue_base}/staff", status_code=303)


def _staff_in_scope(db: Session, request: Request, user_id: uuid.UUID) -> StaffUser:
    """A staff row this venue's page may act on, or 404.

    The same rule the list renders by: a FLOOR account belongs to one
    building; an ADMIN carries NULL, which here means every venue, so they
    are reachable from any venue's page.

    404 rather than 403 -- another venue's casual is not this page's to
    deactivate, and saying "forbidden" would confirm the account exists.
    """
    user = db.get(StaffUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such staff user")
    if user.venue_id is not None and user.venue_id != request.state.venue.id:
        raise HTTPException(status_code=404, detail="No such staff user")
    return user


@router.get("", response_class=HTMLResponse)
def staff_list(
    request: Request,
    welcome: str | None = None,
    outcome: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # THIS VENUE's people, plus the ones who work every venue.
    #
    # StaffUser.venue_id NULL means "every venue" -- the one place a NULL
    # venue means something in this codebase -- so an admin appears on every
    # venue's staff page, correctly, because they do work at every venue.
    # A FLOOR account belongs to one building and appears only there.
    #
    # Until 2026-09-12 this listed every venue's people and every live device
    # token under one venue's URL and one venue's band, which is the
    # mislabelled-page failure this project hit three times in two days.
    venue = request.state.venue
    users = db.scalars(
        select(StaffUser)
        .where(or_(StaffUser.venue_id == venue.id, StaffUser.venue_id.is_(None)))
        .order_by(StaffUser.role, StaffUser.name)
    ).all()

    # Devices are NOT the same question. A phone is in one building, so a
    # token is listed only under its own venue -- an admin who has signed
    # phones into both buildings sees each one on its own page, which is the
    # only way "revoke that device" means something specific.
    tokens_by_user: dict = {}
    for token in db.scalars(
        select(StaffAppToken).where(
            StaffAppToken.revoked_at.is_(None),
            StaffAppToken.venue_id == venue.id,
        )
    ).all():
        tokens_by_user.setdefault(token.staff_user_id, []).append(token)
    return templates.TemplateResponse(
        request,
        "admin/staff_users.html",
        admin_ctx(request, staff, users=users, tokens_by_user=tokens_by_user, me=staff, welcome=welcome, outcome=outcome),
    )


@router.post("/create", dependencies=[Depends(require_csrf)])
def create_staff(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    name, email = name.strip(), email.strip()
    if not name or not email or len(password) < 8:
        raise HTTPException(status_code=422, detail="Name, email and a password of at least 8 characters are required")
    # Whether this ADDS or OVERWRITES, decided before the write. The
    # service is create_OR_UPDATE: an email already on file has its
    # password reset, its role set to whatever is picked here, and
    # is_active forced back to True -- so this form silently reactivates
    # somebody who was deliberately deactivated, and the page then said
    # "account created". That is the only password-reset path there is
    # (no self-serve reset in v1), so the behaviour stays and the screen
    # stops misreporting it.
    existed = staff_auth.get_by_email(db, email) is not None
    try:
        # The venue whose page this form was submitted from -- the only
        # venue it could have meant. A floor account created without one
        # cannot sign into the floor app at all.
        new_user = staff_auth.create_or_update_staff_user(
            db, email=email, name=name, password=password, role=role,
            venue=request.state.venue,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A new floor-team member gets an automatic setup-and-usage email (how
    # to add the app to their phone and how to read it). Admins use the
    # dashboard, not /floor, so they get none. The account is already
    # created regardless -- surface whether the email went so a failure is
    # visible rather than silent.
    outcome = "updated" if existed else "created"
    if new_user.role == "floor":
        sent = notifications.notify_floor_welcome(name=new_user.name, email=new_user.email)
        return RedirectResponse(
            url=f"{request.state.venue_base}/staff?welcome={'sent' if sent else 'failed'}&outcome={outcome}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"{request.state.venue_base}/staff?outcome={outcome}", status_code=303
    )


@router.post("/{user_id}/resend-welcome", dependencies=[Depends(require_csrf)])
def resend_floor_welcome(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    """Re-send the Floor setup-and-usage email -- for a new phone, a lost
    email, etc. Floor accounts only (admins don't use /floor)."""
    user = _staff_in_scope(db, request, user_id)
    if user.role != "floor":
        raise HTTPException(status_code=422, detail="Only floor accounts use the Meantime Floor app")
    sent = notifications.notify_floor_welcome(name=user.name, email=user.email)
    return RedirectResponse(
        url=f"{request.state.venue_base}/staff?welcome={'sent' if sent else 'failed'}",
        status_code=303,
    )


@router.post("/{user_id}/deactivate", dependencies=[Depends(require_csrf)])
def deactivate_staff(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    if user_id == staff.id:
        # Locking every admin out of the admin is one misclick away
        # otherwise; someone else has to deactivate you.
        raise HTTPException(status_code=422, detail="You can't deactivate your own account")
    user = _staff_in_scope(db, request, user_id)
    user.is_active = False
    db.commit()
    return _redirect(request)


@router.post("/{user_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_staff(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    user = _staff_in_scope(db, request, user_id)
    user.is_active = True
    db.commit()
    return _redirect(request)


@router.post("/tokens/{token_id}/revoke", dependencies=[Depends(require_csrf)])
def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    # A token carries its OWN venue -- which building that phone was signed
    # into -- so it is checked against the URL directly rather than through
    # its owner. An admin's phone signed into The Entrance is The
    # Entrance's device to revoke, even though the admin works everywhere.
    token = db.get(StaffAppToken, token_id)
    if token is None or (
        token.venue_id is not None and token.venue_id != request.state.venue.id
    ):
        raise HTTPException(status_code=404, detail="No such device token")
    try:
        staff_auth.revoke_app_token(db, token_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _redirect(request)
