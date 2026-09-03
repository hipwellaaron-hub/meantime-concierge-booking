"""Access control for the AI integration (Phase 1 brief sections 7 and 8).

The boundary this enforces is deliberately dull: a bearer token, two kill
switches, and two rate limits. The *real* boundary is which endpoints
exist at all -- a Tier 3 action has no route, so no amount of instruction,
prompt injection or credential theft can reach it (brief section 2).

Three decisions worth knowing:

- The kill switch lives in the database, checked on every request. An
  environment variable would need a rebuild to change (~90 seconds on
  Railway), which is not a kill switch. The env vars remain a backstop and
  either source saying False wins.
- Write rate limiting counts rows in ai_request_log rather than an
  in-process counter, so tripping the limit survives a restart. An
  auto-disable that quietly reset itself on the next deploy would be worse
  than having no limiter.
- Reads are logged to ai_request_log, never BookingEvent. BookingEvent is
  the audit trail staff read and the dashboard renders; a few hundred
  reads a day would bury the writes in it.
"""

import datetime as dt
import hmac
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AI_ACTOR, AiRequestKind, AiRequestLog, AiSettings, Venue

logger = logging.getLogger(__name__)


class AiAccessError(Exception):
    """Raised when the AI may not proceed. `status` is the HTTP code the
    API layer should return; `detail` is safe to show the caller."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- settings singleton -------------------------------------------------


def get_settings_row(db: Session) -> AiSettings:
    """The single ai_settings row, created on first use. The migration
    seeds it, so this only self-heals a database restored without it."""
    row = db.get(AiSettings, 1)
    if row is None:
        row = AiSettings(id=1, access_enabled=True, writes_enabled=True)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def access_enabled(db: Session) -> bool:
    """Both locks must be open. Env False is a hard override."""
    if not settings.ai_access_enabled:
        return False
    return get_settings_row(db).access_enabled


def writes_enabled(db: Session) -> bool:
    if not settings.ai_writes_enabled:
        return False
    row = get_settings_row(db)
    return row.access_enabled and row.writes_enabled


def set_writes_enabled(
    db: Session, enabled: bool, *, actor: str, reason: str | None = None
) -> AiSettings:
    """Staff toggle, and the limiter's own auto-disable. Records why and
    when, so 'writes are off' is never a mystery."""
    row = get_settings_row(db)
    row.writes_enabled = enabled
    row.updated_by = actor
    if enabled:
        row.writes_disabled_at = None
        row.writes_disabled_reason = None
    else:
        row.writes_disabled_at = dt.datetime.now(dt.timezone.utc)
        row.writes_disabled_reason = reason
    db.commit()
    db.refresh(row)
    return row


def set_access_enabled(db: Session, enabled: bool, *, actor: str) -> AiSettings:
    row = get_settings_row(db)
    row.access_enabled = enabled
    row.updated_by = actor
    db.commit()
    db.refresh(row)
    return row


# --- credential ---------------------------------------------------------


def token_matches(presented: str | None) -> bool:
    """Constant-time compare. An unset AI_API_TOKEN never matches, so a
    deployment that hasn't deliberately configured one is closed, not
    open."""
    expected = settings.ai_api_token or ""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def bearer_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def ai_venue(db: Session) -> Venue:
    """The one venue this credential may see (brief section 7). Scoped now
    so adding the Entrance later is a new credential, not a new filter
    bolted onto every query."""
    venue = db.scalar(select(Venue).where(Venue.slug == settings.ai_venue_slug))
    if venue is None:
        raise AiAccessError(503, f"AI venue '{settings.ai_venue_slug}' is not configured")
    return venue


# --- request log --------------------------------------------------------


def log_request(
    db: Session,
    *,
    kind: AiRequestKind,
    endpoint: str,
    method: str,
    params: dict | None = None,
    status_code: int | None = 200,
    booking_id: uuid.UUID | None = None,
    trigger: str | None = None,
    context: str | None = None,
    actor: str = AI_ACTOR,
) -> AiRequestLog:
    entry = AiRequestLog(
        kind=kind.value,
        endpoint=endpoint,
        method=method,
        params=params or None,
        status_code=status_code,
        booking_id=booking_id,
        trigger=trigger,
        context=context,
        actor=actor,
    )
    db.add(entry)
    db.commit()
    return entry


def count_writes_since(db: Session, since: dt.datetime) -> int:
    return int(
        db.scalar(
            select(func.count(AiRequestLog.id)).where(
                AiRequestLog.kind == AiRequestKind.write.value,
                AiRequestLog.at >= since,
            )
        )
        or 0
    )


def write_budget(db: Session) -> dict:
    """Current consumption against both windows -- surfaced to staff so the
    limit is visible before it trips, not only after."""
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "hour_used": count_writes_since(db, now - dt.timedelta(hours=1)),
        "hour_limit": settings.ai_write_rate_per_hour,
        "day_used": count_writes_since(db, now - dt.timedelta(days=1)),
        "day_limit": settings.ai_write_rate_per_day,
    }


def enforce_write_budget(db: Session) -> None:
    """Raises AiAccessError and turns writes off if either window is spent.

    This is a runaway guard, not an operational ceiling: realistic volume
    is under ten writes a day, so tripping it means something is wrong and
    a human should look before writes resume. Hence auto-disable rather
    than a rolling window that quietly heals itself.
    """
    budget = write_budget(db)
    breach = None
    if budget["hour_used"] >= budget["hour_limit"]:
        breach = f"{budget['hour_used']} writes in the last hour (limit {budget['hour_limit']})"
    elif budget["day_used"] >= budget["day_limit"]:
        breach = f"{budget['day_used']} writes in the last day (limit {budget['day_limit']})"

    if breach:
        set_writes_enabled(db, False, actor="system:rate_limit", reason=breach)
        logger.error("AI write rate limit tripped: %s -- writes auto-disabled", breach)
        raise AiAccessError(
            429,
            f"AI write rate limit exceeded: {breach}. Writes are now disabled until a staff member re-enables them.",
        )
