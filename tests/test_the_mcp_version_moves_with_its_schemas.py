"""The MCP server's version moves when its tool schemas do.

`SERVER_VERSION` is reported by /health and by `initialize`, and it is the
ONLY way to tell which build a connector is talking to. That matters here
more than it usually would, because the two services must be deployed in a
particular order:

    the MCP service first, then the web service.

The web API refuses an AI read that does not name a venue. An MCP build that
does not send `venue` therefore gets a 400 on every read -- Hamilton's
included -- until it catches up. Sending an argument an older API ignores is
harmless; the reverse is a total lockout, and this project has already had a
sibling service sit NINE DAYS stale without anybody noticing.

Commit 25b9671 added the required `venue` argument to four tools and left
SERVER_VERSION at "1.1.0", so /health would have reported the same number
before and after the redeploy -- the check that was supposed to prove the
ordering could not have proved anything.

This is a snapshot gate, like tests/route_inventory.txt. It is not a rule
about what the schemas may contain; it forces a schema change to be a
DELIBERATE, VERSIONED one. When it fails:

    1. read the diff and decide whether callers are affected,
    2. bump SERVER_VERSION in mcp_server/app.py,
    3. regenerate the snapshot in the same commit:

       .venv/Scripts/python.exe -c "from tests.test_the_mcp_version_moves_with_its_schemas import write_snapshot; write_snapshot()"
"""
import hashlib
import json
import pathlib

from mcp_server.app import SERVER_VERSION
from mcp_server.tools import public_tools

SNAPSHOT = pathlib.Path(__file__).parent / "mcp_schema_fingerprint.txt"


def _fingerprint() -> str:
    """A stable hash of every published tool name and input schema.

    Descriptions are included on purpose: a tool description is what the
    model reads to decide how to call it, so changing one changes the
    server's behaviour as surely as changing a property does.
    """
    payload = json.dumps(
        [{"name": t["name"], "description": t.get("description"), "inputSchema": t["inputSchema"]}
         for t in sorted(public_tools(), key=lambda t: t["name"])],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_snapshot() -> None:
    SNAPSHOT.write_text(
        "# The published MCP tool schemas, fingerprinted, and the version that\n"
        "# describes them. Regenerate ONLY together with a SERVER_VERSION bump.\n"
        f"version={SERVER_VERSION}\n"
        f"schemas={_fingerprint()}\n",
        encoding="utf-8",
    )


def _recorded() -> dict[str, str]:
    out = {}
    for line in SNAPSHOT.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


def test_a_schema_change_without_a_version_bump_is_refused():
    assert SNAPSHOT.exists(), f"{SNAPSHOT} is missing -- regenerate it with write_snapshot()"
    recorded = _recorded()

    schemas_changed = _fingerprint() != recorded.get("schemas")
    version_changed = SERVER_VERSION != recorded.get("version")

    if schemas_changed and not version_changed:
        raise AssertionError(
            f"the published MCP tool schemas changed while SERVER_VERSION stayed at "
            f"{SERVER_VERSION!r}.\n"
            "A connector cannot then tell the new build from the old one, and the MCP "
            "service has to be PROVEN live before the web service starts requiring the "
            "new argument. Bump SERVER_VERSION and regenerate the snapshot in the same "
            "commit:\n"
            "  .venv/Scripts/python.exe -c \"from tests.test_the_mcp_version_moves_with_its_schemas import write_snapshot; write_snapshot()\""
        )

    assert not schemas_changed and not version_changed, (
        "the schemas or the version moved; regenerate the snapshot in this commit"
    )


def test_the_four_read_tools_still_require_a_venue():
    """The thing the version is guarding. Stated separately so that
    regenerating the snapshot can never quietly retire it."""
    required_venue = {
        t["name"] for t in public_tools()
        if "venue" in (t["inputSchema"].get("required") or [])
    }

    assert required_venue == {"availability", "bookings", "pipeline", "catalogue"}, (
        f"the set of venue-requiring read tools changed: {sorted(required_venue)}"
    )
