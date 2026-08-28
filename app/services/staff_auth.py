"""Staff login credential handling. No public registration path exists
anywhere in this app -- accounts are provisioned by app.create_staff_user,
run by a human, never by an HTTP route.
"""

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.staff_user import StaffUser


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False  # malformed hash -- never a match


def get_by_email(db: Session, email: str) -> StaffUser | None:
    return db.execute(
        select(StaffUser).where(func.lower(StaffUser.email) == email.strip().lower())
    ).scalar_one_or_none()


def authenticate(db: Session, email: str, password: str) -> StaffUser | None:
    staff = get_by_email(db, email)
    if staff is None or not staff.is_active:
        return None
    if not verify_password(password, staff.password_hash):
        return None
    return staff


def create_or_update_staff_user(
    db: Session, *, email: str, name: str, password: str, role: str = "admin"
) -> StaffUser:
    if role not in ("admin", "floor"):
        raise ValueError(f"unknown staff role {role!r}")
    staff = get_by_email(db, email)
    if staff is None:
        staff = StaffUser(
            email=email.strip().lower(), name=name, password_hash=hash_password(password),
            is_active=True, role=role,
        )
        db.add(staff)
    else:
        staff.name = name
        staff.password_hash = hash_password(password)
        staff.is_active = True
        staff.role = role
    db.commit()
    db.refresh(staff)
    return staff


def deactivate_staff_user(db: Session, *, email: str) -> StaffUser:
    staff = get_by_email(db, email)
    if staff is None:
        raise ValueError(f"No staff user with email {email}")
    staff.is_active = False
    db.commit()
    db.refresh(staff)
    return staff


# --- Meantime Floor app tokens -----------------------------------------------


def _hash_token(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_app_token(db: Session, staff: StaffUser) -> str:
    """Returns the RAW token -- shown to the app once at login, never
    stored; only its sha256 lands in the database."""
    import secrets

    from app.models.staff_app_token import StaffAppToken

    raw = secrets.token_urlsafe(32)
    db.add(StaffAppToken(staff_user_id=staff.id, token_hash=_hash_token(raw)))
    db.commit()
    return raw


def get_staff_by_app_token(db: Session, raw: str) -> StaffUser | None:
    """Resolve a bearer token to its (active) staff user. None for a
    revoked token or a deactivated account -- deactivating a person kills
    every token they hold without touching the token rows."""
    import datetime as dt

    from app.models.staff_app_token import StaffAppToken

    token = db.execute(
        select(StaffAppToken).where(StaffAppToken.token_hash == _hash_token(raw))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    staff = db.get(StaffUser, token.staff_user_id)
    if staff is None or not staff.is_active:
        return None
    token.last_used_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return staff


def revoke_app_token(db: Session, token_id) -> None:
    import datetime as dt

    from app.models.staff_app_token import StaffAppToken

    token = db.get(StaffAppToken, token_id)
    if token is None:
        raise ValueError("No such app token")
    if token.revoked_at is None:
        token.revoked_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
