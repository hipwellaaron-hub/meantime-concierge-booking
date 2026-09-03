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


def complete(*, system: str, user: str, max_tokens: int = 1200) -> str:
    """One prompt in, the assistant's text out. Raises ClaudeUnavailable
    on any failure: missing key, network, timeout, non-200, or a response
    with no text in it."""
    if not is_configured():
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set, so drafting is off.")

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
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            timeout=settings.ai_draft_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise ClaudeUnavailable(
            f"The model did not answer within {settings.ai_draft_timeout_seconds:.0f}s."
        ) from exc
    except httpx.RequestError as exc:
        raise ClaudeUnavailable(f"Could not reach the model ({exc.__class__.__name__}).") from exc

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
