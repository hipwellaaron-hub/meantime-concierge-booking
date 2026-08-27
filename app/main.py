from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse

from app.admin_auth import NotAuthenticated
from app.api.admin_auth import router as admin_auth_router
from app.api.admin_bookings import router as admin_bookings_router
from app.api.admin_calendar import router as admin_calendar_router
from app.api.admin_dashboard import router as admin_dashboard_router
from app.api.admin_reports import router as admin_reports_router
from app.api.admin_triage import router as admin_triage_router
from app.api.availability import router as availability_router
from app.api.documents import router as documents_router
from app.api.enquiries import router as enquiries_router
from app.api.health import router as health_router
from app.api.internal_maintenance import router as internal_maintenance_router
from app.api.invoices import router as invoices_router
from app.api.webhooks import router as webhooks_router
from app.api.wizard import router as wizard_router
from app.config import settings

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


app = FastAPI(title="Meantime Concierge")
app.add_middleware(MaxBodySizeMiddleware, max_size=MAX_BODY_SIZE)
# Signs the staff session cookie (app/admin_auth.py). https_only mirrors
# session_cookie_secure -- see app/config.py for why that's overridable.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.session_cookie_secure)


@app.exception_handler(NotAuthenticated)
def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    # A browser-driven dashboard, not an API client -- an unauthenticated
    # staff route should land back on the login form, not a bare 401.
    return RedirectResponse(url=f"/admin/login?next={request.url.path}", status_code=303)


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(availability_router)
app.include_router(health_router)
app.include_router(documents_router)
app.include_router(invoices_router)
app.include_router(enquiries_router)
app.include_router(webhooks_router)
app.include_router(wizard_router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(admin_bookings_router)
app.include_router(admin_triage_router)
app.include_router(admin_calendar_router)
app.include_router(admin_reports_router)
app.include_router(internal_maintenance_router)


@app.get("/health")
def health():
    return {"status": "ok"}
