"""The Concierge MCP server: OAuth flow, MCP protocol, and the boundary.

The boundary tests matter most. This server adds a surface, not a
permission: it can reach only /api/ai/*, it holds the Concierge credential
without ever emitting it, and it inherits every kill switch -- when
Concierge says AI access is off, every tool stops working and this server
cannot override that.
"""

import base64
import hashlib
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_server import oauth
from mcp_server.app import app
from mcp_server.concierge import ConciergeError, call_ai
from mcp_server.config import settings

PASSWORD = "test-mcp-password"
SIGNING = "test-signing-secret-not-for-production"
PUBLIC = "https://mcp.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "mcp_password", PASSWORD)
    monkeypatch.setattr(settings, "mcp_signing_secret", SIGNING)
    monkeypatch.setattr(settings, "public_url", PUBLIC)
    monkeypatch.setattr(settings, "ai_api_token", "concierge-token-must-never-leak")


@pytest.fixture()
def client():
    return TestClient(app)


def _pkce():
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _connect(client) -> str:
    """The whole flow claude.ai performs: register, sign in, exchange."""
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce()
    resp = client.post(
        "/authorize",
        data={
            "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
            "code_challenge": challenge, "scope": "concierge:read", "password": PASSWORD,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    code = httpx.URL(resp.headers["location"]).params["code"]

    tok = client.post(
        "/token",
        data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200
    return tok.json()["access_token"]


def _rpc(client, token, method, params=None, request_id=1):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


# --- discovery and OAuth ------------------------------------------------


def test_metadata_documents_are_published(client):
    pr = client.get("/.well-known/oauth-protected-resource").json()
    assert pr["resource"] == f"{PUBLIC}/mcp"
    assert pr["authorization_servers"] == [PUBLIC]

    aus = client.get("/.well-known/oauth-authorization-server").json()
    assert aus["authorization_endpoint"] == f"{PUBLIC}/authorize"
    assert aus["code_challenge_methods_supported"] == ["S256"]


def test_mcp_without_a_token_is_401_pointing_at_the_metadata(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["WWW-Authenticate"]


def test_full_oauth_flow_yields_a_working_token(client):
    token = _connect(client)
    assert _rpc(client, token, "tools/list").status_code == 200


def test_wrong_password_does_not_issue_a_code(client):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT]})
    _, challenge = _pkce()
    resp = client.post(
        "/authorize",
        data={
            "client_id": reg.json()["client_id"], "redirect_uri": REDIRECT, "state": "s",
            "code_challenge": challenge, "password": "wrong",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "code=" not in resp.text


def test_unregistered_redirect_uri_is_refused(client):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT]})
    resp = client.get(
        "/authorize",
        params={
            "client_id": reg.json()["client_id"],
            "redirect_uri": "https://evil.example/steal",
            "response_type": "code",
        },
    )
    assert resp.status_code == 400


def test_pkce_mismatch_is_refused(client):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = _pkce()
    resp = client.post(
        "/authorize",
        data={"client_id": client_id, "redirect_uri": REDIRECT, "state": "s",
              "code_challenge": challenge, "password": PASSWORD},
        follow_redirects=False,
    )
    code = httpx.URL(resp.headers["location"]).params["code"]

    bad = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
              "client_id": client_id, "code_verifier": "b" * 64},
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"


def test_a_forged_token_is_rejected(client):
    forged = oauth._sign({"sub": "attacker", "exp": 9999999999}, "access").split(".")[0] + ".AAAA"
    resp = _rpc(client, forged, "tools/list")
    assert resp.status_code == 401


def test_refresh_token_returns_a_new_access_token(client):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    r = client.post(
        "/authorize",
        data={"client_id": client_id, "redirect_uri": REDIRECT, "state": "s",
              "code_challenge": challenge, "password": PASSWORD},
        follow_redirects=False,
    )
    code = httpx.URL(r.headers["location"]).params["code"]
    first = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
              "client_id": client_id, "code_verifier": verifier},
    ).json()

    second = client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}
    )
    assert second.status_code == 200
    assert second.json()["access_token"]


# --- MCP protocol -------------------------------------------------------


def test_initialize_reports_protocol_and_server(client):
    token = _connect(client)
    body = _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "meantime-concierge"
    assert "availability" in body["result"]["instructions"]


def test_tools_list_exposes_every_endpoint_with_a_description(client):
    token = _connect(client)
    tools = _rpc(client, token, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"pipeline", "availability", "bookings", "catalogue"} <= names
    for tool in tools:
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"
        assert "_call" not in tool, "internal dispatch must not be exposed"


def test_availability_description_states_the_things_that_prevent_errors(client):
    token = _connect(client)
    tools = {t["name"]: t for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    text = tools["availability"]["description"].lower()
    assert "open_enquiries" in text or "open enquiries" in text
    assert "tentative" in text
    assert "time-aware" in text
    assert "day_of_week" in text


def test_notifications_get_no_response_body(client):
    token = _connect(client)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert resp.status_code == 202


def test_unknown_tool_is_a_protocol_error(client):
    token = _connect(client)
    body = _rpc(client, token, "tools/call", {"name": "delete_everything", "arguments": {}}).json()
    assert body["error"]["code"] == -32602


# --- calling through to Concierge --------------------------------------


def test_tool_call_forwards_to_concierge_and_returns_its_json(client):
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.get") as mocked:
        mocked.return_value = httpx.Response(
            200, json={"as_of": "2026-09-03T00:00:00+00:00", "days": [{"date": "2026-11-28"}]}
        )
        body = _rpc(
            client, token, "tools/call",
            {"name": "availability", "arguments": {"date": "2026-11-28"}},
        ).json()

    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["days"][0]["date"] == "2026-11-28"

    called_url = mocked.call_args[0][0]
    assert called_url == "https://book.meantime.com.au/api/ai/availability"
    assert mocked.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")


def test_the_concierge_kill_switch_stops_every_tool(client):
    """This server inherits the switch; it cannot override it."""
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.get") as mocked:
        mocked.return_value = httpx.Response(503, json={"detail": "AI access is currently disabled"})
        body = _rpc(client, token, "tools/call", {"name": "pipeline", "arguments": {}}).json()

    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"]
    assert "kill switch" in text.lower()


def test_a_rate_limit_is_reported_not_swallowed(client):
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.get") as mocked:
        mocked.return_value = httpx.Response(429, json={"detail": "rate limited"})
        body = _rpc(client, token, "tools/call", {"name": "pipeline", "arguments": {}}).json()
    assert body["result"]["isError"] is True
    assert "rate limit" in body["result"]["content"][0]["text"].lower()


def test_the_concierge_credential_never_appears_in_a_response(client):
    """The whole point of the server holding it."""
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.get") as mocked:
        mocked.return_value = httpx.Response(200, json={"ok": True})
        body = _rpc(client, token, "tools/call", {"name": "pipeline", "arguments": {}})
    assert settings.ai_api_token not in body.text

    for path in ["/health", "/.well-known/oauth-authorization-server",
                 "/.well-known/oauth-protected-resource"]:
        assert settings.ai_api_token not in client.get(path).text


# --- the boundary -------------------------------------------------------


def test_paths_outside_the_ai_surface_are_refused_before_any_request():
    """Belt and braces: even if a future tool were written carelessly, the
    credential is never attached to an admin route."""
    for path in ["/admin/bookings", "/api/ai/../admin", "/d/sometoken", "/api/ai/bookings/x/status"]:
        with pytest.raises(ConciergeError, match="outside the permitted"):
            call_ai(path)


def test_no_write_tool_exists_yet(client):
    """Writes land when the Concierge endpoints do -- not before."""
    token = _connect(client)
    tools = _rpc(client, token, "tools/list").json()["result"]["tools"]
    forbidden = {"send", "create", "update", "delete", "record_payment", "status"}
    for tool in tools:
        assert not any(word in tool["name"] for word in forbidden), tool["name"]


def test_health_reports_configuration_without_leaking_it(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["configured"] is True
    assert body["tools"] == 7
