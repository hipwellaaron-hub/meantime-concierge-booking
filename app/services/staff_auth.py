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


def create_or_update_staff_user(db: Session, *, email: str, name: str, password: str) -> StaffUser:
    staff = get_by_email(db, email)
    if staff is None:
        staff = StaffUser(email=email.strip().lower(), name=name, password_hash=hash_password(password), is_active=True)
        db.add(staff)
    else:
        staff.name = name
        staff.password_hash = hash_password(password)
        staff.is_active = True
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
