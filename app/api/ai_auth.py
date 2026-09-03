"""The gate every /api/ai/* endpoint sits behind (brief section 7).

Deliberately thin. The security model is not this file -- it is the
absence of Tier 3 routes (brief section 2). This just decides whether a
caller is the AI, whether the switches are on, and records that it asked.

Failure codes are chosen so the AI can tell them apart:
  401  the token is missing or wrong
  503  access (or writes) are switched off, or the venue isn't configured
  429  a rate limit tripped
  404  the action does not exist -- never returned from here, because a
       Tier 3 endpoint has no route to return anything (brief section 12)
"""

import datetime as dt
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AI_ACTOR, AiRequestKind, Venue
from app.rate_limit import InMemoryRateLimiter
from app.services import ai_access

# Reads get an in-memory guard: a restart resetting it is harmless, and it
# exists only to stop a loop hammering the database. Writes are counted in
# the database instead (see ai_access.enforce_write_budget).
_read_limiter = InMemoryRateLimiter(
    max_requests=settings.ai_read_rate_per_min, window_seconds=60
)


@dataclass
class AiContext:
    """What an AI endpoint is allowed to see, plus the read timestamp every
    response must carry so the caller can apply the freshness rule in
    brief section 10a.4."""

    venue: Venue
    actor: str
    as_of: dt.datetime
    db: Session

    @property
    def as_of_iso(self) -> str:
        return self.as_of.isoformat()


def _params(request: Request) -> dict:
    return dict(request.query_params) or {}


def require_ai(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> AiContext:
    """Authenticate the AI credential and confirm access is switched on.

    Every read endpoint depends on this, so every read is logged (at low
    verbosity -- endpoint and params only) and every read is refused the
    instant the database kill switch flips, with no rebuild.
    """
    presented = ai_access.bearer_from_header(authorization)
    if not ai_access.token_matches(presented):
        raise HTTPException(status_code=401, detail="Invalid or missing AI credential")

    if not ai_access.access_enabled(db):
        raise HTTPException(
            status_code=503,
            detail="AI access is currently disabled",
        )

    if not _read_limiter.check(AI_ACTOR):
        raise HTTPException(status_code=429, detail="AI read rate limit exceeded")

    try:
        venue = ai_access.ai_venue(db)
    except ai_access.AiAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    ai_access.log_request(
        db,
        kind=AiRequestKind.read,
        endpoint=request.url.path,
        method=request.method,
        params=_params(request),
    )

    return AiContext(
        venue=venue,
        actor=AI_ACTOR,
        as_of=dt.datetime.now(dt.timezone.utc),
        db=db,
    )


def require_ai_write(ctx: AiContext = Depends(require_ai)) -> AiContext:
    """Additional gate for the Tier 1 safe writes (brief section 4).

    Not used by any endpoint yet -- the three safe writes are a later step
    in the build order -- but the boundary exists before the endpoints do,
    which was the point of building this first.
    """
    if not ai_access.writes_enabled(ctx.db):
        raise HTTPException(status_code=503, detail="AI writes are currently disabled")
    try:
        ai_access.enforce_write_budget(ctx.db)
    except ai_access.AiAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return ctx
