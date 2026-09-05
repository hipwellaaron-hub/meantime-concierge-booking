"""The one place Concierge talks to Claude.

A thin call to the Messages API over httpx rather than the SDK -- httpx is
already a dependency, the surface used here is one POST, and a function
this small is easy to replace in a test with a fake.

Everything that can go wrong raises ClaudeUnavailable, which the drafting
service catches and records. Nothing here may ever propagate into an
enquiry: the rule that governs Phase 2 is that drafting cannot fail an
enquiry, and this module honours it by being the thing that is allowed to
fail, loudly and in one place.

The API key is read at call time and never logged, never echoed into an
error message, and never returned.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class ClaudeUnavailable(Exception):
    """The model could not be used. Message is safe to store and display."""


def is_configured() -> bool:
    return bool(settings.anthropic_api_key)


def _api_key() -> str:
    """The key as it can actually be sent.

    An API key is one run of printable ASCII, so any other character in
    the variable is a paste artefact. The 2026-09-04 fix stripped the
    ENDS of every setting; a key pasted wrapped across two lines keeps a
    line break in the MIDDLE, and h11 refuses to send a header value
    containing one ("Illegal header value" -> LocalProtocolError). Every
    drafting attempt from then on failed with a message that said the
    model was unreachable, and it was seen only after wave 2 went live
    (2026-09-06). Anything that is not printable ASCII is dropped here,
    and the drop is logged as a count, never a value. A key that is
    genuinely wrong then fails as HTTP 401, which says what it is."""
    raw = settings.anthropic_api_key or ""
    clean = "".join(ch for ch in raw if 0x21 <= ord(ch) <= 0x7E)
    if clean != raw:
        logger.warning(
            "ANTHROPIC_API_KEY contained %d character(s) that cannot be sent in a header "
            "(line breaks, spaces or non-ASCII); they were dropped. Re-paste the key in Railway.",
            len(raw) - len(clean),
        )
    return clean


def complete(*, system: str, user: str, max_tokens: int = 1200) -> str:
    """One prompt in, the assistant's text out. Raises ClaudeUnavailable
    on any failure: missing key, network, timeout, non-200, or a response
    with no text in it."""
    if not is_configured():
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set, so drafting is off.")
    key = _api_key()
    if not key:
        raise ClaudeUnavailable(
            "ANTHROPIC_API_KEY contains nothing that can be sent in a header. Re-paste it in Railway."
        )

    body = {
        "model": settings.ai_draft_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        response = httpx.post(
            MESSAGES_URL,
            json=body,
            headers={
                "x-api-key": key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            timeout=settings.ai_draft_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise ClaudeUnavailable(
            f"The model did not answer within {settings.ai_draft_timeout_seconds:.0f}s."
        ) from exc
    except httpx.LocalProtocolError as exc:
        # Reached only if a header is still malformed after _api_key: say
        # what to do, not just what failed.
        raise ClaudeUnavailable(
            "The request could not be formed (LocalProtocolError): a header value is malformed, "
            "which almost always means ANTHROPIC_API_KEY was pasted with a line break. Re-paste it in Railway."
        ) from exc
    except httpx.RequestError as exc:
        raise ClaudeUnavailable(f"Could not reach the model ({exc.__class__.__name__}).") from exc
    except UnicodeEncodeError as exc:
        raise ClaudeUnavailable(
            "A request header contains a non-ASCII character. Re-paste ANTHROPIC_API_KEY in Railway."
        ) from exc

    if response.status_code != 200:
        # Never include the response body verbatim: an auth error can echo
        # request headers. The status is enough to diagnose.
        raise ClaudeUnavailable(f"The model returned HTTP {response.status_code}.")

    try:
        parts = response.json().get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except ValueError as exc:
        raise ClaudeUnavailable("The model returned a response that was not JSON.") from exc

    if not text:
        raise ClaudeUnavailable("The model returned no text.")
    return text
