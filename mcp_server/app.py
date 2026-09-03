"""Meantime Concierge MCP server.

A remote MCP server over HTTPS, so claude.ai can reach it as a custom
connector. It wraps the Concierge /api/ai/* read endpoints as tools and
nothing else.

What it is NOT: a new permission. Every tool call becomes an ordinary
authenticated request to Concierge, which applies its own kill switches,
rate limits and audit logging exactly as before. Switch AI access off in
Concierge and every tool here stops working; this server cannot turn it
back on, and has no route to anything outside /api/ai/*.

Transport is Streamable HTTP (POST /mcp, JSON responses). Sessions are not
used -- each request stands alone, which is simpler and removes a class of
state bugs with no loss for a read-only server.
"""

import json
import logging

from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mcp_server import oauth
from mcp_server.concierge import ConciergeError
from mcp_server.config import is_configured, settings
from mcp_server.tools import call_tool, public_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_NAME = "meantime-concierge"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

app = FastAPI(title="Meantime Concierge MCP", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    return {"status": "ok", "configured": is_configured(), "tools": len(public_tools())}


# --- OAuth discovery ----------------------------------------------------


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    return oauth.protected_resource_metadata()


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server_metadata():
    return oauth.authorization_server_metadata()


@app.post("/register")
async def register(request: Request):
    """Dynamic client registration. claude.ai registers itself here before
    the first sign-in; the returned client_id is a signed blob carrying the
    redirect URIs, so nothing needs storing."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    try:
        client_id = oauth.register_client(
            body.get("redirect_uris") or [], body.get("client_name", "")
        )
    except oauth.OAuthError as exc:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status)

    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": body.get("redirect_uris"),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


_SIGN_IN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Meantime Concierge</title>
<style>
  body {{ background:#0e0e0e; color:#f5f0e8; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
         display:flex; min-height:100vh; margin:0; align-items:center; justify-content:center; }}
  .card {{ background:#161616; border:1px solid #2a2a2a; border-radius:14px; padding:32px; width:min(380px,92vw); }}
  h1 {{ font-size:1.15rem; margin:0 0 6px; color:#c9a96e; }}
  p {{ color:#8a8580; font-size:.85rem; line-height:1.5; margin:0 0 20px; }}
  label {{ display:block; font-size:.8rem; margin-bottom:6px; color:#8a8580; }}
  input {{ width:100%; box-sizing:border-box; padding:11px 12px; border-radius:8px;
           border:1px solid #2a2a2a; background:#0e0e0e; color:#f5f0e8; font-size:1rem; }}
  button {{ width:100%; margin-top:16px; padding:11px; border:0; border-radius:8px;
            background:#c9a96e; color:#0e0e0e; font-weight:600; font-size:.95rem; cursor:pointer; }}
  .err {{ color:#d4622b; font-size:.82rem; margin-bottom:14px; }}
</style></head>
<body><form class="card" method="post" action="/authorize">
  <h1>Meantime Concierge</h1>
  <p>Sign in to connect Claude to Concierge. This grants read access to bookings,
     availability and the menu. It cannot change anything.</p>
  {error}
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="scope" value="{scope}">
  <label for="p">Access password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Connect</button>
</form></body></html>"""


def _render_sign_in(*, client_id, redirect_uri, state, code_challenge, scope, error=""):
    from html import escape

    return _SIGN_IN_PAGE.format(
        client_id=escape(client_id or ""),
        redirect_uri=escape(redirect_uri or ""),
        state=escape(state or ""),
        code_challenge=escape(code_challenge or ""),
        scope=escape(scope or ""),
        error=f'<div class="err">{escape(error)}</div>' if error else "",
    )


@app.get("/authorize", response_class=HTMLResponse)
def authorize_form(
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
    scope: str = "concierge:read",
    response_type: str = "code",
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge and code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "S256 required"}, status_code=400)
    try:
        allowed = oauth.client_redirect_uris(client_id)
    except oauth.OAuthError as exc:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=400)
    if redirect_uri not in allowed:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Unregistered redirect_uri"},
            status_code=400,
        )
    return HTMLResponse(
        _render_sign_in(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, scope=scope,
        )
    )


@app.post("/authorize")
def authorize_submit(
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    scope: str = Form("concierge:read"),
    password: str = Form(""),
):
    try:
        allowed = oauth.client_redirect_uris(client_id)
    except oauth.OAuthError as exc:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=400)
    if redirect_uri not in allowed:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    if not oauth.password_matches(password):
        return HTMLResponse(
            _render_sign_in(
                client_id=client_id, redirect_uri=redirect_uri, state=state,
                code_challenge=code_challenge, scope=scope,
                error="That password was not correct.",
            ),
            status_code=401,
        )

    code = oauth.issue_code(client_id, redirect_uri, code_challenge, scope)
    separator = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{separator}code={code}"
    if state:
        target += f"&state={state}"
    return RedirectResponse(target, status_code=303)


@app.post("/token")
def token(
    grant_type: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
):
    try:
        if grant_type == "authorization_code":
            payload = oauth.redeem_code(code, client_id, redirect_uri, code_verifier)
            return oauth.issue_tokens(scope=payload.get("scope", ""))
        if grant_type == "refresh_token":
            return oauth.refresh_tokens(refresh_token)
        raise oauth.OAuthError("unsupported_grant_type", f"Unsupported grant_type {grant_type!r}")
    except oauth.OAuthError as exc:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=exc.status if exc.status >= 400 else 400,
        )


# --- MCP endpoint -------------------------------------------------------


def _unauthorized() -> JSONResponse:
    """401 pointing at the metadata, which is how claude.ai discovers where
    to authenticate (RFC 9728)."""
    resource = f"{oauth.issuer()}/.well-known/oauth-protected-resource"
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource}"'},
    )


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle(message: dict) -> dict | None:
    """One JSON-RPC message. Returns None for notifications, which take no
    response."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method is None:
        return _error(request_id, -32600, "Not a request")

    if method == "initialize":
        wanted = params.get("protocolVersion")
        protocol = wanted if wanted in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return _result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Read-only access to Meantime Concierge. Always call a tool rather than "
                    "answering from memory when the question is about availability, a price, "
                    "a payment or a booking's stage. Before saying a date is free, check "
                    "`availability` -- a slot with nothing confirmed may still have open "
                    "enquiries or a tentative hold, and a reply must disclose that."
                ),
            },
        )

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": public_tools()})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            payload = call_tool(name, arguments)
        except KeyError:
            return _error(request_id, -32602, f"Unknown tool {name!r}")
        except ConciergeError as exc:
            # A tool-level failure, reported inside the result so the model
            # sees why and can stop, rather than a transport error.
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed", name)
            return _result(
                request_id,
                {"content": [{"type": "text", "text": f"Tool failed: {exc}"}], "isError": True},
            )

        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
                "isError": False,
            },
        )

    return _error(request_id, -32601, f"Method not found: {method}")


@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: str | None = Header(default=None)):
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
    if not presented:
        return _unauthorized()
    try:
        oauth.verify_access_token(presented)
    except oauth.OAuthError:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

    if isinstance(body, list):
        responses = [r for r in (_handle(m) for m in body) if r is not None]
        if not responses:
            return JSONResponse(None, status_code=202)
        return JSONResponse(responses)

    response = _handle(body)
    if response is None:
        return JSONResponse(None, status_code=202)
    return JSONResponse(response)


@app.get("/mcp")
def mcp_get():
    """No server-initiated streaming: this server only answers requests.
    405 is the spec-sanctioned way to say so."""
    return JSONResponse({"error": "method_not_allowed"}, status_code=405)
