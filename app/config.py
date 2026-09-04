from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # str_strip_whitespace: every string field here gets a Railway variable
    # or a pasted secret at some point, and a copy-paste artifact (a
    # trailing newline, a stray space) is invisible in a dashboard text box
    # but not to a strict consumer -- httpx refuses to send a header value
    # containing a raw newline at all (httpx.LocalProtocolError), which is
    # exactly how a whitespace-contaminated ANTHROPIC_API_KEY surfaced,
    # 2026-09-04: every drafting attempt recorded "Could not reach the
    # model (LocalProtocolError)" even though the key itself was correct.
    # Same root cause as the Gmail app-password fix in
    # app.services.notifications._strip_all_whitespace; fixed here once,
    # for every field, rather than per-field as each one bites.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", str_strip_whitespace=True)

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

    # Public web-measurement identifiers for the enquiry funnel. Both PUBLIC
    # (not secrets) and both default EMPTY so local/dev/test never loads a
    # real tag or pollutes production analytics with test traffic -- the
    # templates render the GA4 / Meta Pixel snippets only when the matching
    # id is set. Production (book.meantime.com.au) sets these to the
    # confirmed Meantime values via Railway variables:
    #   GA4_MEASUREMENT_ID = G-XM8C86CGM6   (same GA4 property as meantime.com.au)
    #   META_PIXEL_ID      = 7461755457239404 (existing MeantimeHamilton pixel)
    ga4_measurement_id: str = ""
    meta_pixel_id: str = ""

    # --- AI integration (Phase 1 brief) ---------------------------------
    # Bearer token for /api/ai/*. Empty by default so a deployment that
    # hasn't deliberately set one refuses every AI request rather than
    # running open. Never in code, rotatable without a code change (section 7).
    ai_api_token: str = ""

    # Env-level backstop for the kill switches. The real switch is the
    # ai_settings row (instant, no rebuild); these are the second lock, and
    # either source saying False wins -- so a switch can't be re-enabled by
    # editing only one of them.
    ai_access_enabled: bool = True
    ai_writes_enabled: bool = True

    # The AI credential is venue-scoped from day one even though Hamilton
    # is the only venue -- the Entrance gets its own credential later (section 7).
    ai_venue_slug: str = "hamilton"

    # Runaway guards, not operational ceilings (section 4.4, section 7). Reads are
    # in-memory (a restart resetting a read guard is harmless); writes are
    # counted in the database so an auto-disable survives a restart.
    ai_read_rate_per_min: int = 300
    ai_write_rate_per_hour: int = 20
    ai_write_rate_per_day: int = 100

    # --- Phase 2 drafting -----------------------------------------------
    # Empty by default: with no key, every drafting attempt records
    # 'skipped' and nothing is called. Never logged, never echoed.
    anthropic_api_key: str = ""
    ai_draft_model: str = "claude-sonnet-5"
    # A slow model must never hold anything up. By the time drafting runs
    # the enquiry is saved and staff are notified, so a timeout here costs
    # one missing draft and nothing else.
    ai_draft_timeout_seconds: float = 30.0


settings = Settings()
