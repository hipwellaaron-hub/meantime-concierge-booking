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
    db: Session, *, email: str, name: str, password: str, role: str = "admin", venue=None
) -> StaffUser:
    """Create or update an account.

    `venue` is which building a FLOOR account works at, and it matters: a
    floor account with no venue cannot sign into the floor app at all --
    venue_for_token has nothing to give it and no right to guess. The admin
    form passes the venue whose page it was submitted from, which is the
    only venue that page could have meant.

    An ADMIN is always left with NULL, whatever is passed: NULL means every
    venue, which is what an admin is, and pinning one to a building would
    quietly stop them opening the other.
    """
    if role not in ("admin", "floor"):
        raise ValueError(f"unknown staff role {role!r}")
    venue_id = venue.id if (venue is not None and role == "floor") else None
    staff = get_by_email(db, email)
    if staff is None:
        staff = StaffUser(
            email=email.strip().lower(), name=name, password_hash=hash_password(password),
            is_active=True, role=role, venue_id=venue_id,
        )
        db.add(staff)
    else:
        staff.name = name
        staff.password_hash = hash_password(password)
        staff.is_active = True
        staff.role = role
        # An existing account's venue follows its role: promoting somebody to
        # admin clears it (they now work everywhere), and re-creating a floor
        # account on the page for a different venue moves them there, which
        # is the only sensible reading of doing that.
        staff.venue_id = venue_id
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


class VenueRequired(ValueError):
    """Raised when a token is issued without saying which venue the device is
    for. Carries the venues the person may choose from, so the caller can
    ask rather than guess."""

    def __init__(self, message: str, choices):
        super().__init__(message)
        self.choices = list(choices)


def venue_for_token(db: Session, staff: StaffUser, venue_slug: str | None):
    """Which venue a new floor device belongs to.

    A FLOOR account names its venue and cannot be overridden -- passing a
    slug that is not theirs is refused rather than honoured, so a casual who
    works one building cannot put the other building's run sheets on their
    phone by editing a request.

    An ADMIN carries no venue (NULL means every venue), so they must say
    which building this phone is in. No default: picking one for them is how
    a device ends up showing the wrong venue's run sheets all night.
    """
    from sqlalchemy import select

    from app.models import Venue

    if staff.venue_id is not None:
        if venue_slug and venue_slug != staff.venue.slug:
            raise VenueRequired(
                "this account is not at that venue", [staff.venue.slug]
            )
        return staff.venue

    choices = [v.slug for v in db.scalars(select(Venue).order_by(Venue.name)).all()]
    if not venue_slug:
        raise VenueRequired("which venue is this device for?", choices)
    venue = db.scalars(select(Venue).where(Venue.slug == venue_slug)).one_or_none()
    if venue is None:
        raise VenueRequired("no such venue", choices)
    return venue


def issue_app_token(db: Session, staff: StaffUser, venue) -> str:
    """Returns the RAW token -- shown to the app once at login, never
    stored; only its sha256 lands in the database.

    `venue` is REQUIRED and has no default. A floor token is what puts one
    venue's run sheets on a phone for the rest of the night, and a device
    that guessed would be wrong silently.
    """
    import secrets

    from app.models.staff_app_token import StaffAppToken

    if venue is None:
        raise VenueRequired("a floor token must name its venue", [])

    raw = secrets.token_urlsafe(32)
    db.add(StaffAppToken(
        staff_user_id=staff.id, token_hash=_hash_token(raw), venue_id=venue.id
    ))
    db.commit()
    return raw


def get_token(db: Session, raw: str):
    """The TOKEN row for a raw bearer value, or None.

    Callers need the token and not just its owner, because the token is what
    carries the venue -- which building this particular phone was signed
    into. get_staff_by_app_token below discards it, which is exactly how a
    phone signed into The Entrance ended up showing Hamilton's run sheets.
    """
    import datetime as dt

    from app.models.staff_app_token import StaffAppToken

    token = db.execute(
        select(StaffAppToken).where(StaffAppToken.token_hash == _hash_token(raw))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.venue_id is None:
        # Pre-venue or rolled-back token: refuse, do not guess which venue's
        # run sheets to put on this phone.
        return None
    if not token.staff_user.is_active:
        return None
    token.last_used_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return token


def get_staff_by_app_token(db: Session, raw: str) -> StaffUser | None:
    """Resolve a bearer token to its (active) staff user. None for a
    revoked token or a deactivated account -- deactivating a person kills
    every token they hold without touching the token rows.

    A token with NO VENUE is refused. That means it predates
    staff_app_tokens.venue_id or was minted by a rolled-back build, and a
    device holding one cannot be told which venue's run sheets to show. The
    honest answer is to make it sign in again rather than pick a venue for
    it -- the app already handles a 401 by clearing the token and showing
    the sign-in screen.
    """
    import datetime as dt

    from app.models.staff_app_token import StaffAppToken

    token = db.execute(
        select(StaffAppToken).where(StaffAppToken.token_hash == _hash_token(raw))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.venue_id is None:
        # Pre-venue or rolled-back token: refuse, do not guess.
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
