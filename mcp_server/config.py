"""Configuration for the Concierge MCP server.

This process is deliberately separate from the Concierge app. It holds one
credential (the AI bearer token) and can reach exactly one thing: the
Concierge /api/ai/* endpoints, over HTTPS. It has no database connection,
no session secret, and no import of any Concierge service module -- so
"the MCP server can only reach /api/ai/*" is a property of what it is,
not a rule it promises to follow.

It adds a surface, not a permission. Every kill switch, rate limit and
audit rule from the AI brief lives in Concierge and applies unchanged: if
AI access is switched off there, this server's tools stop working, and it
cannot re-enable them.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- talking to Concierge -------------------------------------------
    # The only host this process ever calls.
    concierge_base_url: str = "https://book.meantime.com.au"
    # The Concierge AI credential. Empty by default, so a deployment that
    # has not deliberately been given one cannot reach anything -- the same
    # closed-by-default rule the Concierge side uses.
    ai_api_token: str = ""
    concierge_timeout_seconds: float = 20.0

    # --- callers authenticating to THIS server --------------------------
    # claude.ai connects over OAuth. The only human credential is this
    # password, typed on this server's own sign-in page; the Concierge AI
    # token is never sent to a client and never leaves this process.
    mcp_password: str = ""
    # Signs OAuth artefacts (client ids, auth codes, access/refresh
    # tokens). Rotating it invalidates every existing connection, which is
    # the intended emergency behaviour.
    mcp_signing_secret: str = ""

    # This server's own public HTTPS origin, used to build the OAuth
    # metadata documents. Must match what claude.ai is pointed at.
    public_url: str = ""

    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    auth_code_ttl_seconds: int = 120


settings = Settings()


def is_configured() -> bool:
    """Every secret present. Reported by /health so a half-configured
    deployment is obvious rather than mysteriously refusing OAuth."""
    return all(
        [
            settings.ai_api_token,
            settings.mcp_password,
            settings.mcp_signing_secret,
            settings.public_url,
        ]
    )
