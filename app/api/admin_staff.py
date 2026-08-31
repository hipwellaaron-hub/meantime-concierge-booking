"""Staff account management for Concierge admin -- the page the Meantime
Floor brief requires: floor accounts are created, disabled and their app
tokens revoked from here, never via a public route. Until now accounts
only existed via the one-off CLI (app.create_staff_user); that still
works and is unchanged.
"""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import StaffAppToken, StaffUser
from app.services import notifications, staff_auth
from app.templating import templates

router = APIRouter(prefix="/admin/staff", tags=["admin-staff"], dependencies=[Depends(require_staff)])


def _redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/staff", status_code=303)


@router.get("", response_class=HTMLResponse)
def staff_list(
    request: Request,
    welcome: str | None = None,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    users = db.scalars(select(StaffUser).order_by(StaffUser.role, StaffUser.name)).all()
    tokens_by_user: dict = {}
    for token in db.scalars(select(StaffAppToken).where(StaffAppToken.revoked_at.is_(None))).all():
        tokens_by_user.setdefault(token.staff_user_id, []).append(token)
    return templates.TemplateResponse(
        request,
        "admin/staff_users.html",
        admin_ctx(request, staff, users=users, tokens_by_user=tokens_by_user, me=staff, welcome=welcome),
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
    try:
        new_user = staff_auth.create_or_update_staff_user(db, email=email, name=name, password=password, role=role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A new floor-team member gets an automatic setup-and-usage email (how
    # to add the app to their phone and how to read it). Admins use the
    # dashboard, not /floor, so they get none. The account is already
    # created regardless -- surface whether the email went so a failure is
    # visible rather than silent.
    if new_user.role == "floor":
        sent = notifications.notify_floor_welcome(name=new_user.name, email=new_user.email)
        return RedirectResponse(url=f"/admin/staff?welcome={'sent' if sent else 'failed'}", status_code=303)
    return _redirect()


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
    user = db.get(StaffUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such staff user")
    user.is_active = False
    db.commit()
    return _redirect()


@router.post("/{user_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_staff(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    user = db.get(StaffUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such staff user")
    user.is_active = True
    db.commit()
    return _redirect()


@router.post("/tokens/{token_id}/revoke", dependencies=[Depends(require_csrf)])
def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    try:
        staff_auth.revoke_app_token(db, token_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _redirect()
