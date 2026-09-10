from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse

from app.admin_auth import NotAuthenticated
from app.api.ai_read import router as ai_read_router
from app.api.ai_write import router as ai_write_router
from app.api.admin_auth import router as admin_auth_router
from app.api.admin_bookings import router as admin_bookings_router
from app.api.admin_calendar import router as admin_calendar_router
from app.api.admin_dashboard import router as admin_dashboard_router
from app.api.admin_drafts import router as admin_drafts_router
from app.api.admin_invoices import router as admin_invoices_router
from app.api.admin_reports import router as admin_reports_router
from app.api.admin_staff import router as admin_staff_router
from app.api.admin_triage import router as admin_triage_router
from app.api.availability import router as availability_router
from app.api.documents import router as documents_router
from app.api.enquiries import router as enquiries_router
from app.api.floor_app import router as floor_app_router
from app.api.health import router as health_router
from app.api.invoices import router as invoices_router
from app.api.staff_app import router as staff_app_router
from app.api.webhooks import router as webhooks_router
from app.api.wizard import router as wizard_router
from app.config import settings
from app.templating import templates

# Generous for the JSON/form payloads this app actually receives (an
# enquiry, a signature) -- blocks gross abuse, not real use. Only catches
# requests that declare an honest Content-Length; a client deliberately
# using chunked transfer-encoding to omit it could still stream an
# unbounded body. Acceptable for a single small venue's public forms;
# would need a streaming byte-count guard to be airtight.
MAX_BODY_SIZE = 200_000


class MaxBodySizeMiddleware:
    def __init__(self, app, max_size: int):
        self.app = app
        self.max_size = max_size

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    too_large = int(content_length) > self.max_size
                except ValueError:
                    too_large = False
                if too_large:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app = FastAPI(
    title="Meantime Concierge",
    # Interactive API docs are closed unless explicitly enabled (see
    # settings.expose_api_docs): a private booking system shouldn't publish
    # a full route/parameter map to anyone who asks.
    docs_url="/docs" if settings.expose_api_docs else None,
    redoc_url="/redoc" if settings.expose_api_docs else None,
    openapi_url="/openapi.json" if settings.expose_api_docs else None,
)


class SecurityHeadersMiddleware:
    """Baseline hardening headers on every response. None of these change
    behaviour for a well-behaved browser; they close off clickjacking
    (X-Frame-Options), MIME-sniffing (X-Content-Type-Options), referer
    leakage of tokened URLs to third parties (Referrer-Policy), and pin
    HTTPS for return visits (HSTS). A full Content-Security-Policy is
    deliberately not set here yet -- the wizard and floor pages rely on
    inline <script> blocks, so a CSP needs per-page nonces to avoid
    breaking them; tracked as a follow-up rather than shipped half-done.
    """

    _HEADERS = {
        b"x-frame-options": b"SAMEORIGIN",
        b"x-content-type-options": b"nosniff",
        b"referrer-policy": b"strict-origin-when-cross-origin",
        b"strict-transport-security": b"max-age=31536000; includeSubDomains",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {k.lower() for k, _ in headers}
                for key, value in self._HEADERS.items():
                    if key not in existing:
                        headers.append((key, value))
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxBodySizeMiddleware, max_size=MAX_BODY_SIZE)
# Signs the staff session cookie (app/admin_auth.py). https_only mirrors
# session_cookie_secure -- see app/config.py for why that's overridable.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.session_cookie_secure)


@app.exception_handler(NotAuthenticated)
def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    # A browser-driven dashboard, not an API client -- an unauthenticated
    # staff route should land back on the login form, not a bare 401.
    # quote(): the path is attacker-chosen, and an unencoded "&" or "#" in it
    # would inject further parameters into the login URL (2026-09-07 review).
    return RedirectResponse(url=f"/admin/login?next={quote(request.url.path, safe='/')}", status_code=303)


# Headings that say what happened, per status. The detail itself carries
# the specifics -- these only set the tone so a 404 does not read like a
# refusal and a refusal does not read like a crash.
_ERROR_HEADINGS = {
    403: "Not allowed",
    404: "Not found",
    409: "That can't be done right now",
    422: "That can't be saved",
}


def _safe_admin_back(request: Request) -> str:
    """Where the Back button goes: the page they came from, if it is one.

    PATH ONLY, and only an /admin one. The Referer header is chosen by
    whoever made the request, and putting it into a link unvalidated is
    the open redirect this codebase already had to fix once on
    /admin/login (2026-09-07 review).
    """
    try:
        path = urlparse(request.headers.get("referer") or "").path
    except ValueError:
        return "/admin"
    return path if path.startswith("/admin/") else "/admin"


@app.exception_handler(StarletteHTTPException)
async def _admin_errors_get_a_page(request: Request, exc: StarletteHTTPException):
    """A staff member who hits a refusal gets a page with a way back.

    Every admin refusal raises HTTPException, and the default renders it
    as {"detail": "..."} on an otherwise blank page. In the desktop app
    there is no browser chrome and no Back button, so that is a DEAD END --
    the only way out is to close and reopen the app. Aaron hit it on
    2026-09-10 saving an agreed minimum without a reason, and there are 82
    other raises in the admin routers that would each do the same.

    This is the reasoning the NotAuthenticated handler above already
    applies to a 401, extended to the rest: a browser-driven dashboard is
    not an API client.

    /admin ONLY. Everything under /api still answers JSON exactly as
    before -- the AI read API and the MCP both depend on seeing the real
    status and detail rather than an HTML page, which mcp_server/concierge
    says out loud ("the model should see exactly that and stop, not
    receive an empty result it might read as 'nothing found'"). The floor
    app under /api/staff is JSON for the same reason.
    """
    if not request.url.path.startswith("/admin"):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else "That request could not be completed."
    return templates.TemplateResponse(
        request,
        "admin/error.html",
        {
            "heading": _ERROR_HEADINGS.get(exc.status_code, "Something went wrong"),
            "message": detail,
            "back_url": _safe_admin_back(request),
            # A 4xx refused before doing anything. A 5xx may have recorded
            # the attempt (a resend writes its outcome to the trail before
            # reporting it), so it must not claim nothing changed.
            "nothing_changed": exc.status_code < 500,
        },
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def _admin_validation_errors_get_a_page(request: Request, exc: RequestValidationError):
    """The same rule for a form that arrived incomplete (a missing field,
    a value of the wrong shape): under /admin that is a page with a way
    back, not {"detail": [...]}. /api and the public routes keep JSON."""
    if not request.url.path.startswith("/admin"):
        return await request_validation_exception_handler(request, exc)
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path"))
        problems.append(f"{where}: {error.get('msg', 'invalid')}" if where else str(error.get("msg", "invalid")))
    return templates.TemplateResponse(
        request,
        "admin/error.html",
        {
            "heading": _ERROR_HEADINGS[422],
            "message": "The form was missing something or had a value the server could not read -- " + "; ".join(problems),
            "back_url": _safe_admin_back(request),
            "nothing_changed": True,
        },
        status_code=422,
    )


app.mount("/static", StaticFiles(directory="app/static"), name="static")


# Root-level icon conventions so every page (and iOS home-screen) gets the
# brand mark without touching each template's <head>. Browsers request these
# fixed paths automatically; long-cache since the assets are content-stable.
_ICON_CACHE = {"Cache-Control": "public, max-age=604800"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("app/static/icons/favicon.ico", headers=_ICON_CACHE)


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse("app/static/icons/apple-touch-icon.png", headers=_ICON_CACHE)


@app.get("/site.webmanifest", include_in_schema=False)
def site_webmanifest():
    return FileResponse("app/static/site.webmanifest", media_type="application/manifest+json")


@app.get("/browserconfig.xml", include_in_schema=False)
def browserconfig():
    # Legacy Windows/IE tile pinning -- browsers request this fixed root
    # path automatically, same convention as favicon.ico and site.webmanifest.
    return FileResponse("app/static/icons/browserconfig.xml", media_type="application/xml", headers=_ICON_CACHE)

app.include_router(ai_read_router)
app.include_router(ai_write_router)
app.include_router(availability_router)
app.include_router(health_router)
app.include_router(documents_router)
app.include_router(invoices_router)
app.include_router(enquiries_router)
app.include_router(webhooks_router)
app.include_router(wizard_router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(admin_drafts_router)
app.include_router(admin_bookings_router)
app.include_router(admin_invoices_router)
app.include_router(admin_triage_router)
app.include_router(admin_calendar_router)
app.include_router(admin_reports_router)
app.include_router(admin_staff_router)
app.include_router(staff_app_router)
app.include_router(floor_app_router)


@app.get("/health")
def health():
    return {"status": "ok"}
