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
from mcp_server.tools import BY_NAME, ToolArgumentError, call_tool, public_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_NAME = "meantime-concierge"
# 1.2.0: `venue` became a REQUIRED argument on the four read tools
# (availability, bookings, pipeline, catalogue). That is a breaking change
# for a caller, so it is a version, and the version is what tells you which
# build the connector is actually talking to.
#
# THE VERSION MUST MOVE WITH A SCHEMA CHANGE. The venue argument shipped in
# 25b9671 with this number left at 1.1.0, which made /health useless for the
# one thing it is for: confirming the redeploy landed before the web service
# starts requiring the argument. Deploying those two out of order locks out
# every AI read, Hamilton's included.
#
# 1.1.0: the Event Order proposal tools (one read, one write). The version
# is reported by /health and by initialize, so a redeploy can be verified
# from the connector side without guessing.
SERVER_VERSION = "1.2.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

app = FastAPI(title="Meantime Concierge MCP", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    # The version belongs here as well as in `initialize`. The comment above
    # has claimed since 1.1.0 that /health reports it; it did not, so the
    # documented way to verify an MCP redeploy ("check /health says 1.1.0")
    # could never have worked, and every check of it fell back to guessing
    # from the build log. Proved against the live service, 2026-09-11.
    return {
        "status": "ok",
        "version": SERVER_VERSION,
        "configured": is_configured(),
        "tools": len(public_tools()),
    }


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
     availability and the menu, and lets Claude <em>propose</em> Event Order wording
     for staff to approve. It cannot change a booking, a document, a status or a
     figure, and nothing it proposes is applied until a staff member approves it.</p>
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
    if "id" not in message:
        # A notification, whatever its method: JSON-RPC and MCP both say
        # it gets no response, and an error envelope with id null is a
        # response.
        return None

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
                    "Access to Meantime Concierge: every tool reads, except "
                    "`propose_event_order_values`, which writes a PROPOSAL that a staff member "
                    "must approve field by field before anything reaches the Event Order. "
                    "Always call a tool rather than answering from memory when the question "
                    "is about availability, a price, a payment or a booking's stage. Before "
                    "saying a date is free, check `availability` -- a slot with nothing "
                    "confirmed may still have open enquiries or a tentative hold, and a reply "
                    "must disclose that. Before proposing, read `event_order_proposal` and "
                    "the current Event Order so nothing already declared is dropped."
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
        arguments = params.get("arguments")  # None means absent; [] or 0 is a caller's mistake, said as such
        if not isinstance(name, str) or name not in BY_NAME:
            return _error(
                request_id, -32602, f"Unknown tool {name!r}. The tools are: " + ", ".join(BY_NAME)
            )
        try:
            payload = call_tool(name, arguments)
        except ToolArgumentError as exc:
            # A bad ARGUMENT, said as such. It used to surface as "Unknown
            # tool", which sent the caller looking for a deployment problem
            # instead of its own guessed field name.
            return _error(request_id, -32602, f"Tool {name!r}: {exc}")
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
        if not body:
            return JSONResponse(_error(None, -32600, "Invalid Request: empty batch"), status_code=200)
        responses = [
            r
            for r in (_handle(m) if isinstance(m, dict) else _error(None, -32600, "Invalid Request") for m in body)
            if r is not None
        ]
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
