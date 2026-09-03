"""OAuth 2.1 for the MCP endpoint -- the pattern claude.ai expects.

Stateless on purpose. Every artefact (client id, authorization code,
access and refresh token) is a signed, self-describing blob rather than a
row somewhere, which means this process needs no database at all. That
keeps the security surface honest: the server holds two secrets and one
credential and stores nothing.

The only human credential is MCP_PASSWORD, typed on this server's own
sign-in page. The Concierge AI token is never sent to a client, never
appears in a redirect, and never leaves this process.

Rotating MCP_SIGNING_SECRET invalidates every issued token and every
registered client at once, which is the intended emergency behaviour.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from mcp_server.config import settings


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: dict, kind: str) -> str:
    """A compact signed token: base64(json).base64(hmac). `kind` is inside
    the payload and checked on verify, so an access token can never be
    replayed as a refresh token or an authorization code."""
    if not settings.mcp_signing_secret:
        raise OAuthError("server_error", "This server has no signing secret configured.", 503)
    body = dict(payload, kind=kind)
    encoded = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(
        settings.mcp_signing_secret.encode(), encoded.encode(), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64(signature)}"


def _verify(token: str, kind: str) -> dict:
    if not settings.mcp_signing_secret:
        raise OAuthError("server_error", "This server has no signing secret configured.", 503)
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise OAuthError("invalid_token", "Malformed token.", 401) from exc

    expected = hmac.new(
        settings.mcp_signing_secret.encode(), encoded.encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64(expected), signature):
        raise OAuthError("invalid_token", "Bad signature.", 401)

    try:
        payload = json.loads(_unb64(encoded))
    except Exception as exc:  # noqa: BLE001
        raise OAuthError("invalid_token", "Unreadable token.", 401) from exc

    if payload.get("kind") != kind:
        raise OAuthError("invalid_token", "Token used for the wrong purpose.", 401)
    if payload.get("exp") and payload["exp"] < time.time():
        raise OAuthError("invalid_token", "Token has expired.", 401)
    return payload


# --- dynamic client registration ---------------------------------------
# The client id IS the registration: a signed blob carrying the redirect
# URIs. Nothing is stored, so a restart never orphans a client claude.ai
# has already registered.


def register_client(redirect_uris: list[str], client_name: str = "") -> str:
    if not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "At least one redirect_uri is required.")
    return _sign(
        {"redirect_uris": redirect_uris, "name": client_name[:120], "iat": int(time.time())},
        "client",
    )


def client_redirect_uris(client_id: str) -> list[str]:
    return _verify(client_id, "client").get("redirect_uris", [])


# --- authorization code (PKCE) -----------------------------------------


def issue_code(client_id: str, redirect_uri: str, code_challenge: str, scope: str = "") -> str:
    return _sign(
        {
            "client_id_digest": hashlib.sha256(client_id.encode()).hexdigest()[:32],
            "redirect_uri": redirect_uri,
            "cc": code_challenge,
            "scope": scope,
            "nonce": secrets.token_urlsafe(8),
            "exp": int(time.time()) + settings.auth_code_ttl_seconds,
        },
        "code",
    )


def redeem_code(code: str, client_id: str, redirect_uri: str, code_verifier: str) -> dict:
    payload = _verify(code, "code")

    if payload["client_id_digest"] != hashlib.sha256(client_id.encode()).hexdigest()[:32]:
        raise OAuthError("invalid_grant", "This code was issued to a different client.")
    if payload["redirect_uri"] != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the one used to authorize.")

    challenge = payload.get("cc")
    if challenge:
        if not code_verifier:
            raise OAuthError("invalid_grant", "code_verifier is required.")
        derived = _b64(hashlib.sha256(code_verifier.encode()).digest())
        if not hmac.compare_digest(derived, challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed.")
    return payload


# --- access and refresh tokens -----------------------------------------


def issue_tokens(scope: str = "") -> dict:
    now = int(time.time())
    access = _sign(
        {"sub": "meantime-staff", "scope": scope, "exp": now + settings.access_token_ttl_seconds},
        "access",
    )
    refresh = _sign(
        {"sub": "meantime-staff", "scope": scope, "exp": now + settings.refresh_token_ttl_seconds},
        "refresh",
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.access_token_ttl_seconds,
        "refresh_token": refresh,
        "scope": scope,
    }


def refresh_tokens(refresh_token: str) -> dict:
    payload = _verify(refresh_token, "refresh")
    return issue_tokens(scope=payload.get("scope", ""))


def verify_access_token(token: str) -> dict:
    return _verify(token, "access")


def password_matches(presented: str) -> bool:
    """Constant-time. An unset password never matches, so an
    unconfigured deployment cannot be signed into."""
    expected = settings.mcp_password or ""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


# --- metadata documents -------------------------------------------------


def issuer() -> str:
    return settings.public_url.rstrip("/")


def authorization_server_metadata() -> dict:
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Public client with PKCE: there is no client secret to leak.
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["concierge:read"],
    }


def protected_resource_metadata() -> dict:
    base = issuer()
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["concierge:read"],
        "bearer_methods_supported": ["header"],
    }
