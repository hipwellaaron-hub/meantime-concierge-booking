from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    test_database_url: str = ""

    # Signs the staff session cookie (Starlette's SessionMiddleware). No
    # default -- fails loudly at startup rather than ever signing a real
    # session with a guessable key. Set once per environment (local .env,
    # Railway variable) and never needs to change afterward.
    secret_key: str
    # Secure flag on the session cookie -- must be True in production
    # (Railway serves HTTPS), but a plain http://localhost dev server
    # can't set a Secure cookie, so this is overridable to False locally.
    session_cookie_secure: bool = True

    # How many trusted proxies sit in front of the app, for deriving the
    # real client IP from X-Forwarded-For (see app/rate_limit.py). The
    # LEFTMOST XFF entries are client-supplied and spoofable; only the
    # rightmost `trusted_proxy_hops` entries were appended by
    # infrastructure we trust. Railway fronts the app with a single edge
    # proxy, so the real client is the last XFF entry -> default 1. Set to
    # 0 to ignore XFF entirely and use the raw socket peer (correct only
    # with no proxy, e.g. some local setups). Getting this wrong doesn't
    # breach anything; too-low lets a client spoof their rate-limit bucket,
    # too-high collapses distinct clients into one bucket.
    trusted_proxy_hops: int = 1

    # Guided Booking Wizard auto-routing. Both default OFF: a clean wizard
    # submission (no [REVIEW] markers, no escalation) is technically ready
    # to generate/send automatically, but "nothing client-facing auto-
    # sends" is a project-wide rule -- these flags exist so Aaron can
    # decide to relax it deliberately, per document (BEO is internal/staff-
    # facing, the invoice is client-facing, so they're independently
    # switchable rather than one combined flag).
    wizard_beo_auto_finalize: bool = False
    wizard_invoice_auto_send: bool = False

    # FastAPI's interactive API docs (/docs, /redoc, /openapi.json). Off by
    # default: this is a private booking system, and an open schema just
    # hands a stranger a complete map of every route and parameter. The
    # endpoints behind it are auth-gated regardless, so this is defence in
    # depth, not the lock itself. Flip to True in a local .env if you want
    # the Swagger UI while developing.
    expose_api_docs: bool = False

    # The one public URL every internal notification email (enquiry
    # notification, staff digest) links back into -- a single place to
    # change it rather than the same literal duplicated in
    # app.services.notifications and app.send_digest.
    dashboard_base_url: str = "https://meantime-concierge-booking-production.up.railway.app"


settings = Settings()
