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
import re

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
ALLOWED_PATH_SUFFIXES = ("/documents", "/invoices", "/events", "/event-order-proposal")

# The write surface, kept separate from the read one on purpose: post_ai
# checks this list and call_ai checks the one above, so a read tool cannot
# be talked into performing a write and a write tool cannot quietly widen
# into anything else. Exactly one entry today.
ALLOWED_POST_SUFFIXES = ("/event-order-proposal",)


class ConciergeError(Exception):
    """A call to Concierge failed. The message is safe to hand to the
    model: it explains what happened without exposing the credential."""


# A booking reference (HAM-20271114-AB12C) or a UUID, and nothing else. The
# model chooses these values, and they are interpolated into a URL path.
#
# Without this gate the allowlist below is decorative: it tests the string
# with startswith/endswith, but httpx normalises the URL afterwards, so
# "../../../admin/foo?x" passes both checks and the real request goes to
# /admin/foo carrying the Concierge credential. Verified against the pinned
# httpx, 2026-09-07 review. "?" alone is enough -- it pushes the suffix into
# the query string -- and "#" truncates the path entirely.
#
# So outside input never reaches a path unchecked: it must be one segment of
# unreserved characters (RFC 3986), which needs no encoding and cannot carry
# a separator, a dot segment, or a percent escape.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")


def path_segment(value: object, *, name: str = "identifier") -> str:
    """One URL path segment built from a value chosen outside this process."""
    text = str(value if value is not None else "").strip()
    if not _SAFE_SEGMENT.match(text) or text.strip(".") == "":
        raise ConciergeError(
            f"Refused: {name} must be a plain booking reference or id "
            f"(letters, digits, dot, dash, underscore, tilde); got {text[:60]!r}"
        )
    return text


def _well_formed(path: str) -> bool:
    """Defence in depth: even a path this module built itself must contain no
    separator or dot segment beyond the literal route."""
    return (
        "?" not in path
        and "#" not in path
        and "%" not in path
        and "//" not in path
        and not any(part in (".", "..") for part in path.split("/"))
    )


def _path_allowed(path: str) -> bool:
    if not _well_formed(path):
        return False
    if path in ALLOWED_PATHS:
        return True
    if path.startswith("/api/ai/bookings/") and path.endswith(ALLOWED_PATH_SUFFIXES):
        return True
    return False


def _post_path_allowed(path: str) -> bool:
    if not _well_formed(path):
        return False
    return path.startswith("/api/ai/bookings/") and path.endswith(ALLOWED_POST_SUFFIXES)


def _raise_for_status(response, path: str) -> None:
    """Concierge's own words, passed through. A model that is told "the
    proposal failed the house rules, here are the codes" can fix it; one
    that is told "something went wrong" retries blind."""
    if response.status_code == 401:
        raise ConciergeError(
            "Concierge rejected this server's credential. The AI token is missing, wrong, or rotated."
        )
    if response.status_code == 503:
        raise ConciergeError(
            "Concierge has AI access switched off. This is the kill switch; nothing can be read or "
            "proposed until a staff member re-enables it in Concierge."
        )
    if response.status_code == 429:
        raise ConciergeError(
            "Concierge rate limit or AI write budget reached. Writes may have been disabled "
            "automatically; a staff member re-enables them. Do not retry."
        )
    if response.status_code == 404:
        raise ConciergeError("Not found in Concierge.")
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("detail", body)
        except Exception:  # noqa: BLE001 -- a non-JSON error body is not worth crashing on
            detail = response.text[:500]
        raise ConciergeError(f"Concierge returned {response.status_code}: {detail}")


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

    _raise_for_status(response, path)

    try:
        return response.json()
    except ValueError as exc:
        raise ConciergeError("Concierge returned a response that was not JSON.") from exc


def post_ai(path: str, body: dict) -> dict:
    """POST a Concierge AI write endpoint and return the parsed JSON.

    Deliberately a separate function from call_ai with a separate
    allowlist. The only write Concierge exposes is proposing Event Order
    values, and a proposal applies nothing: every field waits for a staff
    approval on the Event Order form. There is no endpoint that approves,
    and none is wrapped here.
    """
    if not _post_path_allowed(path):
        raise ConciergeError(f"Refused: {path} is not a permitted Concierge write")
    if not settings.ai_api_token:
        raise ConciergeError(
            "This MCP server has no Concierge credential configured, so it cannot propose anything."
        )

    url = settings.concierge_base_url.rstrip("/") + path
    try:
        response = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {settings.ai_api_token}",
                "Accept": "application/json",
            },
            timeout=settings.concierge_timeout_seconds,
        )
    except httpx.RequestError as exc:
        logger.exception("Concierge write failed: %s", path)
        raise ConciergeError(f"Could not reach Concierge ({exc.__class__.__name__}).") from exc

    _raise_for_status(response, path)

    try:
        return response.json()
    except ValueError as exc:
        raise ConciergeError("Concierge returned a response that was not JSON.") from exc
