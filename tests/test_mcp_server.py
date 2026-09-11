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
from mcp_server import tools as tools_module
from mcp_server.app import app
from mcp_server.concierge import ConciergeError, call_ai, post_ai
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


def test_unknown_tool_is_a_protocol_error_that_names_the_tools(client):
    token = _connect(client)
    body = _rpc(client, token, "tools/call", {"name": "delete_everything", "arguments": {}}).json()
    assert body["error"]["code"] == -32602
    assert "Unknown tool 'delete_everything'" in body["error"]["message"]
    assert "propose_event_order_values" in body["error"]["message"], "the refusal lists what does exist"


# --- a bad ARGUMENT is said as such, never as "Unknown tool" -----------------
#
# 2026-09-10, the first live proposal: a guessed field name (catering_notes)
# came back as "Unknown tool 'propose_event_order_values'", which sent the
# caller looking for a deployment problem instead of its own mistake.


def _propose(client, token, arguments):
    with patch("mcp_server.tools.post_ai") as posted:
        posted.return_value = {"proposal_id": "p1", "status": "pending", "awaiting_approval": ["music"]}
        body = _rpc(client, token, "tools/call", {"name": "propose_event_order_values", "arguments": arguments}).json()
    return body, posted


def test_a_guessed_field_name_is_refused_by_name_with_the_valid_ones_listed(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email 10 Sep",
        "fields": {"catering_notes": "grazing", "music_entertainment": "DJ"},
    })

    assert body["error"]["code"] == -32602
    message = body["error"]["message"]
    assert "Unknown tool" not in message
    assert "'catering_notes'" in message and "'music_entertainment'" in message
    assert "catering_order_and_service_style" in message and "onsite_contact" in message, "the valid names"
    assert not posted.called, "nothing reaches Concierge on a refused call"


def test_a_missing_required_argument_is_named(client):
    token = _connect(client)
    body, posted = _propose(client, token, {"reference": "HAM-20260926-FM49Q", "fields": {"music": "DJ"}})

    assert body["error"]["code"] == -32602
    assert "missing required argument 'source'" in body["error"]["message"]
    assert "Unknown tool" not in body["error"]["message"]
    assert not posted.called


def test_an_unexpected_top_level_argument_is_named(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email", "fields": {"music": "DJ"}, "booking_id": "x",
    })

    assert body["error"]["code"] == -32602
    assert "unexpected argument 'booking_id'" in body["error"]["message"]
    assert "valid arguments are: reference, source, fields, food_order, trigger, model" in body["error"]["message"]
    assert not posted.called


def test_a_long_trigger_is_refused_with_the_limit_stated(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email", "fields": {"music": "DJ"},
        "trigger": "client final details, 16 days out, no Event Order existed",
    })

    assert body["error"]["code"] == -32602
    assert "trigger must be at most 30 characters" in body["error"]["message"]
    assert not posted.called


def test_a_read_tool_with_a_missing_argument_is_an_argument_error_not_an_unknown_tool(client):
    """The earlier instance of the same defect: args['booking_id'] raising
    KeyError inside booking_documents was reported as Unknown tool. The
    validator now refuses before the tool runs, naming both mistakes."""
    token = _connect(client)
    body = _rpc(client, token, "tools/call", {"name": "booking_documents", "arguments": {"ref": "HAM-1"}}).json()

    assert body["error"]["code"] == -32602
    assert "Unknown tool" not in body["error"]["message"]
    assert "missing required argument 'booking_id'" in body["error"]["message"]
    assert "unexpected argument 'ref'" in body["error"]["message"]


def test_a_well_formed_proposal_still_goes_through(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email 10 Sep, final details",
        "fields": {"decorations": "Nothing declared", "accessibility": "No accessibility requirements declared"},
        "trigger": "client final details",
    })

    assert "error" not in body, body
    assert posted.called
    assert posted.call_args[0][1]["trigger"] == "client final details"
    assert body["result"]["isError"] is False
    assert json.loads(body["result"]["content"][0]["text"])["proposal_id"] == "p1", "the model sees Concierge's answer"


def test_the_propose_description_states_the_names_and_the_limits(client):
    token = _connect(client)
    tools = {t["name"]: t for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    tool = tools["propose_event_order_values"]

    from app.services.beo_rules import MAX_FIELD_LENGTH, PROPOSABLE_FIELDS

    for name in PROPOSABLE_FIELDS:
        assert name in tool["description"], f"{name} is not named in the description"
        assert tool["inputSchema"]["properties"]["fields"]["properties"][name]["maxLength"] == MAX_FIELD_LENGTH
    assert "30 characters" in tool["description"]
    assert tool["inputSchema"]["properties"]["trigger"]["maxLength"] == 30
    # The rules Concierge enforces that the model can only learn here.
    for code in ("rsa_missing", "rsa_absent_on_document", "legacy_music_split"):
        assert code in tool["description"], code
    assert "request phrasing" in tool["description"], "client-voice rule is wider than first person"
    assert "_call" not in tool


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


def test_the_only_write_is_a_proposal(client):
    """Concierge exposes exactly one write -- proposing Event Order values,
    which applies nothing -- and this server wraps exactly that. No tool
    sends, creates, approves, applies, pays or changes a status, and there
    is no approve endpoint to wrap even if one were wanted."""
    from mcp_server import tools as tools_module

    token = _connect(client)
    tools = _rpc(client, token, "tools/list").json()["result"]["tools"]
    forbidden = {"send", "create", "update", "delete", "record_payment", "status", "approve", "apply"}
    for tool in tools:
        assert not any(word in tool["name"] for word in forbidden), tool["name"]

    writers = [t["name"] for t in tools_module.TOOLS if "post_ai" in t["_call"].__code__.co_names]
    assert writers == ["propose_event_order_values"]


def test_a_proposal_is_posted_to_concierge_with_its_source(client):
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.post") as mocked:
        mocked.return_value = httpx.Response(
            201, json={"proposal_id": "p1", "status": "pending", "awaiting_approval": ["dietaries"]}
        )
        body = _rpc(
            client, token, "tools/call",
            {
                "name": "propose_event_order_values",
                "arguments": {
                    "reference": "HAM-20271114-AB12C",
                    "source": "client email 6 Sep, final details",
                    "fields": {"dietaries": "1x severe nut allergy (table 4)."},
                },
            },
        ).json()

    assert body["result"]["isError"] is False
    assert json.loads(body["result"]["content"][0]["text"])["awaiting_approval"] == ["dietaries"]
    assert mocked.call_args[0][0] == "https://book.meantime.com.au/api/ai/bookings/HAM-20271114-AB12C/event-order-proposal"
    sent = mocked.call_args.kwargs["json"]
    assert sent["source"] == "client email 6 Sep, final details"
    assert sent["fields"] == {"dietaries": "1x severe nut allergy (table 4)."}
    assert mocked.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")
    assert settings.ai_api_token not in body["result"]["content"][0]["text"]


def test_a_refused_proposal_hands_the_model_the_rule_codes(client):
    """A 422 is Concierge saying which house rule failed. The model must see
    the codes -- "something went wrong" makes it retry blind, and a refused
    proposal is never shown to staff, so blind retries help nobody."""
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.post") as mocked:
        mocked.return_value = httpx.Response(
            422,
            json={"detail": {
                "error": "the proposal failed the Event Order house rules",
                "rule_codes": ["dietary_contamination"],
                "violations": [{"code": "dietary_contamination", "field": "dietaries",
                                "message": "Dietaries carries decoration language.", "excerpt": "balloon arch"}],
            }},
        )
        body = _rpc(
            client, token, "tools/call",
            {"name": "propose_event_order_values", "arguments": {
                "reference": "HAM-1", "source": "client email", "fields": {"dietaries": "Balloon arch, no nuts"},
            }},
        ).json()

    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"]
    assert "dietary_contamination" in text
    assert "balloon arch" in text


def test_a_tripped_write_budget_tells_the_model_not_to_retry(client):
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.post") as mocked:
        mocked.return_value = httpx.Response(429, json={"detail": "AI write budget exceeded"})
        body = _rpc(
            client, token, "tools/call",
            {"name": "propose_event_order_values", "arguments": {
                "reference": "HAM-1", "source": "client email", "fields": {"music": "DJ."},
            }},
        ).json()
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"].lower()
    assert "budget" in text and "do not retry" in text


def test_the_read_of_a_proposal_uses_get_not_post(client):
    token = _connect(client)
    with patch("mcp_server.concierge.httpx.get") as get, patch("mcp_server.concierge.httpx.post") as post:
        get.return_value = httpx.Response(200, json={"reference": "HAM-1", "proposal": None})
        body = _rpc(client, token, "tools/call",
                    {"name": "event_order_proposal", "arguments": {"reference": "HAM-1"}}).json()
    assert body["result"]["isError"] is False
    assert get.call_args[0][0].endswith("/api/ai/bookings/HAM-1/event-order-proposal")
    post.assert_not_called()


@pytest.mark.parametrize("hostile", [
    "../../../admin/foo?x",       # httpx normalises this to /admin/foo
    "a/../b",
    "HAM-1?a",                    # "?" pushes the suffix into the query string
    "HAM-1#frag",                 # "#" truncates the path entirely
    "HAM-1/../../admin",
    "%2e%2e%2fadmin",
    "..",
    "",
])
@pytest.mark.parametrize("tool", ["event_order_proposal", "propose_event_order_values", "booking_documents"])
def test_a_reference_can_never_steer_the_request_off_the_ai_surface(hostile, tool):
    """The allowlist tests the path with startswith/endswith, but httpx
    normalises the URL afterwards -- so "../../../admin/foo?x" passed both
    checks and the real request went to /admin/foo carrying the Concierge
    credential (verified 2026-09-07). Outside input now has to be one plain
    segment before it reaches a path at all."""
    key = "booking_id" if tool == "booking_documents" else "reference"
    args = {key: hostile}
    if tool == "propose_event_order_values":
        args |= {"source": "client email", "fields": {"music": "DJ."}}

    with patch("mcp_server.concierge.httpx.get") as get, patch("mcp_server.concierge.httpx.post") as post:
        with pytest.raises(ConciergeError, match="must be a plain booking reference"):
            tools_module.call_tool(tool, args)
        get.assert_not_called()
        post.assert_not_called()


def test_a_real_reference_still_reaches_the_right_path():
    with patch("mcp_server.concierge.httpx.post") as mocked:
        mocked.return_value = httpx.Response(201, json={"proposal_id": "p1"})
        tools_module.call_tool("propose_event_order_values", {
            "reference": "HAM-20271114-AB12C", "source": "client email", "fields": {"music": "DJ."},
        })
    assert mocked.call_args[0][0].endswith("/api/ai/bookings/HAM-20271114-AB12C/event-order-proposal")


def test_post_refuses_every_path_but_the_proposal_before_any_request():
    """The write allowlist is separate from the read one, so widening one
    cannot widen the other. An approve path does not exist in Concierge and
    must be refused here too, so a future careless tool fails loudly."""
    with patch("mcp_server.concierge.httpx.post") as mocked:
        for path in [
            "/api/ai/pipeline",
            "/api/ai/bookings/HAM-1/event-order-proposal/approve",
            "/api/ai/bookings/HAM-1/status",
            "/admin/bookings/HAM-1/documents/beo/generate",
        ]:
            with pytest.raises(ConciergeError, match="not a permitted Concierge write"):
                post_ai(path, {})
        mocked.assert_not_called()

    # And the read function still cannot be pointed at the write.
    with patch("mcp_server.concierge.httpx.get") as mocked:
        mocked.return_value = httpx.Response(200, json={})
        call_ai("/api/ai/bookings/HAM-1/event-order-proposal")  # the GET is a real read
        assert mocked.call_args.kwargs.get("json") is None


def test_health_reports_configuration_without_leaking_it(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["configured"] is True
    assert body["tools"] == 9  # seven reads, the proposal read, and the one write


def test_refresh_tokens_outlive_access_tokens_and_a_late_refresh_still_works():
    """An hour-long access token cost a day of 401s when a client that
    never calls /token kept presenting it (2026-09-09/10). Access tokens
    now last 30 days; refresh tokens must last LONGER, because both are
    stamped from one clock and a refresh presented after the access token
    expired must still find a live refresh token (equal lifetimes died in
    the same second -- review, 2026-09-10). Asserted on the declared
    defaults, so an environment override cannot fail or mask this."""
    from mcp_server import oauth
    from mcp_server.config import Settings, settings

    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    assert defaults["access_token_ttl_seconds"] == 60 * 60 * 24 * 30
    assert defaults["refresh_token_ttl_seconds"] > defaults["access_token_ttl_seconds"]

    t0 = 1_800_000_000
    with (
        patch.object(settings, "mcp_signing_secret", "test-signing-secret"),
        patch.object(settings, "access_token_ttl_seconds", 100),
        patch.object(settings, "refresh_token_ttl_seconds", 300),
    ):
        with patch.object(oauth.time, "time", lambda: t0):
            pair = oauth.issue_tokens()
        with patch.object(oauth.time, "time", lambda: t0 + 101):
            with pytest.raises(oauth.OAuthError):
                oauth.verify_access_token(pair["access_token"])
            fresh = oauth.refresh_tokens(pair["refresh_token"])  # late refresh: the grant it exists for
        with patch.object(oauth.time, "time", lambda: t0 + 102):
            oauth.verify_access_token(fresh["access_token"])
        with patch.object(oauth.time, "time", lambda: t0 + 301):
            with pytest.raises(oauth.OAuthError):
                oauth.refresh_tokens(pair["refresh_token"])


# --- the review of the argument checking ----------------------------------------


def test_the_shape_that_produced_unknown_tool_is_named_twice(client):
    """What the live 2026-09-10 call most likely sent: the field names at
    the top level with no `fields` wrapper. That raised KeyError('fields')
    inside the tool, which the router reported as Unknown tool."""
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email", "catering_notes": "grazing",
    })

    assert body["error"]["code"] == -32602
    assert "unexpected argument 'catering_notes'" in body["error"]["message"]
    assert "valid arguments are: reference, source, fields, food_order" in body["error"]["message"]
    assert "Unknown tool" not in body["error"]["message"]
    assert not posted.called


def test_null_for_an_optional_argument_means_absent(client):
    """Concierge accepts None for trigger and model, and every _call reads
    optionals with .get(); a client that sends null for what it has nothing
    to say about must not be refused (review, 2026-09-11)."""
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-20260926-FM49Q", "source": "client email", "fields": {"music": "DJ"},
        "trigger": None, "model": None,
    })

    assert "error" not in body, body
    assert posted.call_args[0][1]["trigger"] is None and posted.call_args[0][1]["model"] is None


def test_null_for_a_required_argument_is_still_refused(client):
    token = _connect(client)
    body, posted = _propose(client, token, {"reference": "HAM-20260926-FM49Q", "source": None, "fields": {"music": "DJ"}})

    assert body["error"]["code"] == -32602
    assert "source must be a string" in body["error"]["message"]
    assert not posted.called


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"reference": "HAM-1", "source": "client email", "fields": "DJ"}, "fields must be an object"),
        ({"reference": "HAM-1", "source": "client email", "fields": {"music": 5}}, "fields.music must be a string"),
        ({"reference": "HAM-1", "source": "ab", "fields": {"music": "DJ"}}, "source must be at least 3 characters"),
        ({"reference": "HAM-1", "source": "client email", "fields": {"music": "x" * 2001}}, "at most 2000 characters (got 2001)"),
        ({"reference": "HAM-1", "source": "client email", "fields": {"music": "DJ"}, "trigger": "x" * 31}, "trigger must be at most 30 characters"),
    ],
)
def test_each_validator_clause_refuses_with_its_own_words(client, arguments, expected):
    token = _connect(client)
    body, posted = _propose(client, token, arguments)

    assert body["error"]["code"] == -32602
    assert expected in body["error"]["message"], body["error"]["message"]
    assert not posted.called


def test_a_trigger_of_exactly_thirty_characters_is_accepted(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-1", "source": "client email", "fields": {"music": "DJ"}, "trigger": "x" * 30,
    })

    assert "error" not in body, body
    assert posted.call_args[0][1]["trigger"] == "x" * 30


def test_integer_and_enum_arguments_are_checked_too(client):
    token = _connect(client)
    with patch("mcp_server.tools.call_ai") as called:
        limit = _rpc(client, token, "tools/call", {"name": "booking_events", "arguments": {"booking_id": "abc", "limit": "ten"}}).json()
        stage = _rpc(client, token, "tools/call", {"name": "pipeline", "arguments": {"stage": "imaginary"}}).json()

    assert "limit must be an integer" in limit["error"]["message"]
    assert "stage must be one of:" in stage["error"]["message"]
    assert not called.called


@pytest.mark.parametrize("arguments", [[], 0, False, [1], "x"])
def test_arguments_that_are_not_an_object_are_said_so(client, arguments):
    """Including the FALSY shapes ([], 0, false): `params.get("arguments")
    or {}` used to turn those into an empty object and run the tool."""
    token = _connect(client)
    with patch("mcp_server.tools.call_ai") as called:
        body = _rpc(client, token, "tools/call", {"name": "pipeline", "arguments": arguments}).json()

    assert body["error"]["code"] == -32602, body
    assert "arguments must be an object" in body["error"]["message"]
    assert not called.called


def test_a_non_string_tool_name_is_an_error_envelope_not_a_500(client):
    token = _connect(client)
    resp = _rpc(client, token, "tools/call", {"name": ["x"], "arguments": {}})

    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32602
    assert "Unknown tool" in resp.json()["error"]["message"]


def test_every_published_schema_is_closed(client):
    token = _connect(client)
    tools = _rpc(client, token, "tools/list").json()["result"]["tools"]

    assert tools, "no tools listed"
    for tool in tools:
        assert tool["inputSchema"].get("additionalProperties") is False, tool["name"]


def test_an_id_less_tools_call_is_a_notification_and_gets_nothing_back(client):
    token = _connect(client)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "nothing", "arguments": {}}},
    )

    assert resp.status_code == 202
    assert resp.content in (b"", b"null")


def test_batch_edge_cases_are_answered_per_json_rpc(client):
    token = _connect(client)
    empty = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json=[])
    mixed = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json=[{"jsonrpc": "2.0", "id": 7, "method": "ping"}, "junk", {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": ["x"]}}],
    )

    assert empty.status_code == 200 and empty.json()["error"]["code"] == -32600
    assert mixed.status_code == 200
    codes = [(item.get("id"), (item.get("error") or {}).get("code")) for item in mixed.json()]
    assert (7, None) in codes, "the good request in the batch still gets its result"
    assert (None, -32600) in codes, "the non-object item is answered, not crashed on"
    assert (8, -32602) in codes, "the bad tool name is answered in its own envelope"


# --- the food order through the tool -------------------------------------------------


def test_a_food_order_goes_through_the_tool_without_text_fields(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-1", "source": "client email 10 Sep",
        "food_order": [{"name": "Grazing Platter", "quantity": 2}, {"menu_item_id": "abc", "quantity": 1}],
    })

    assert "error" not in body, body
    assert posted.call_args[0][1]["food_order"] == [{"name": "Grazing Platter", "quantity": 2}, {"menu_item_id": "abc", "quantity": 1}]
    assert posted.call_args[0][1]["fields"] == {}


def test_a_food_line_with_a_price_never_leaves_the_tool(client):
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-1", "source": "client email",
        "food_order": [{"name": "Grazing Platter", "quantity": 2, "unit_price": "9.00"}],
    })

    assert body["error"]["code"] == -32602
    assert "unexpected field 'unit_price' in food_order[0]" in body["error"]["message"]
    assert not posted.called


@pytest.mark.parametrize(
    ("food_order", "expected"),
    [
        ([], "food_order must have at least 1 entry"),
        ([{"name": "x", "quantity": 0}], "food_order[0].quantity must be at least 1"),
        ([{"name": "x", "quantity": 501}], "food_order[0].quantity must be at most 500"),
        ([{"name": "x", "quantity": "2"}], "food_order[0].quantity must be an integer"),
        ([{"name": "x"}], "missing required argument 'food_order[0].quantity'"),
        ("Grazing Platter x2", "food_order must be a list"),
    ],
)
def test_food_order_shapes_are_refused_before_sending(client, food_order, expected):
    token = _connect(client)
    body, posted = _propose(client, token, {"reference": "HAM-1", "source": "client email", "food_order": food_order})

    assert body["error"]["code"] == -32602
    assert expected in body["error"]["message"], body["error"]["message"]
    assert not posted.called


def test_the_description_says_how_the_food_order_is_proposed(client):
    token = _connect(client)
    tools = {t["name"]: t for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    tool = tools["propose_event_order_values"]

    for phrase in ("food_order", "NEVER a price", "food_price_sent", "food_unknown_item", "`catalogue`"):
        assert phrase in tool["description"], phrase
    assert "fields" not in tool["inputSchema"]["required"], "a food-only proposal is a proposal"
    items = tool["inputSchema"]["properties"]["food_order"]["items"]
    assert items["additionalProperties"] is False and items["required"] == ["quantity"]


def test_an_empty_fields_object_beside_a_food_order_is_a_proposal(client):
    """The natural food-only call. The MCP refused it while the API
    accepted that exact body (review, 2026-09-11)."""
    token = _connect(client)
    body, posted = _propose(client, token, {
        "reference": "HAM-1", "source": "client email", "fields": {},
        "food_order": [{"name": "Grazing Platter", "quantity": 2}],
    })

    assert "error" not in body, body
    assert posted.call_args[0][1]["fields"] == {}
    assert posted.call_args[0][1]["food_order"] == [{"name": "Grazing Platter", "quantity": 2}]
