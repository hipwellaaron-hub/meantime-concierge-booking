"""The only outbound path this process has: Concierge /api/ai/*.

Every call goes through call_ai(), which refuses any path outside the
allowlist below. That is belt and braces on top of the fact that no other
path is ever constructed -- if a future tool is added carelessly, it fails
here rather than reaching an admin route with the AI credential attached.

Errors are passed through rather than smoothed over. If Concierge says
"AI access is currently disabled" (503) or "rate limit exceeded" (429),
the model should see exactly that and stop, not receive an empty result it
might read as "nothing found".
"""

import logging

import httpx

from mcp_server.config import settings

logger = logging.getLogger(__name__)

# Exactly the read endpoints that exist today. Write endpoints are added
# here as they land in Concierge -- and not before, because a tool whose
# endpoint does not exist would surface as a confusing 404 to the model.
ALLOWED_PATHS = frozenset(
    {
        "/api/ai/pipeline",
        "/api/ai/availability",
        "/api/ai/bookings",
        "/api/ai/catalogue",
    }
)

# Per-booking detail paths, matched by shape rather than literal.
ALLOWED_PATH_SUFFIXES = ("/documents", "/invoices", "/events")


class ConciergeError(Exception):
    """A call to Concierge failed. The message is safe to hand to the
    model: it explains what happened without exposing the credential."""


def _path_allowed(path: str) -> bool:
    if path in ALLOWED_PATHS:
        return True
    if path.startswith("/api/ai/bookings/") and path.endswith(ALLOWED_PATH_SUFFIXES):
        return True
    return False


def call_ai(path: str, params: dict | None = None) -> dict:
    """GET a Concierge AI endpoint and return the parsed JSON.

    Read-only by construction: this function only ever issues GET. When
    the Tier 1 writes land they get their own function with their own
    allowlist, so a read tool can never be talked into performing a write.
    """
    if not _path_allowed(path):
        raise ConciergeError(f"Refused: {path} is outside the permitted /api/ai/* surface")
    if not settings.ai_api_token:
        raise ConciergeError(
            "This MCP server has no Concierge credential configured, so it cannot read anything."
        )

    url = settings.concierge_base_url.rstrip("/") + path
    cleaned = {k: v for k, v in (params or {}).items() if v is not None}

    try:
        response = httpx.get(
            url,
            params=cleaned,
            headers={
                "Authorization": f"Bearer {settings.ai_api_token}",
                "Accept": "application/json",
            },
            timeout=settings.concierge_timeout_seconds,
        )
    except httpx.RequestError as exc:
        logger.exception("Concierge request failed: %s", path)
        raise ConciergeError(f"Could not reach Concierge ({exc.__class__.__name__}).") from exc

    if response.status_code == 401:
        raise ConciergeError(
            "Concierge rejected this server's credential. The AI token is missing, wrong, or rotated."
        )
    if response.status_code == 503:
        raise ConciergeError(
            "Concierge has AI access switched off. This is the kill switch; nothing can be read until "
            "a staff member re-enables it in Concierge."
        )
    if response.status_code == 429:
        raise ConciergeError("Concierge rate limit reached. Wait before querying again.")
    if response.status_code == 404:
        raise ConciergeError("Not found in Concierge.")
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", "")
        except Exception:  # noqa: BLE001 -- a non-JSON error body is not worth crashing on
            detail = response.text[:200]
        raise ConciergeError(f"Concierge returned {response.status_code}: {detail}")

    try:
        return response.json()
    except ValueError as exc:
        raise ConciergeError("Concierge returned a response that was not JSON.") from exc
