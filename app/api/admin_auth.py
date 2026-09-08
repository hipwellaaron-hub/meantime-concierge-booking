from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff, start_session
from app.database import get_db
from app.models.staff_user import StaffUser
from app.rate_limit import InMemoryRateLimiter, rate_limit_dependency
from app.services.staff_auth import authenticate
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin-auth"])

# Same 5-attempts-in-5-minutes flavour of protection as the sign/enquiry
# endpoints -- blunts scripted brute-forcing, not a real login retry.
login_rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=300)


def safe_next(next: str) -> str:
    """Where a login may send the browser: a path inside the admin, never a
    URL someone put in a link.

    Both login routes take `next` straight from the query string or the
    form, so without this an authenticated staff member who clicks
    book.meantime.com.au/admin/login?next=https://evil.example/ is bounced
    off the venue's own domain to whatever the link says -- the credible
    half of a phishing page, hosted by us. The POST already guarded this;
    the GET did not (2026-09-07 review).

    A scheme-relative "//evil.example" fails the prefix test, and a
    same-origin path like "/admin.evil.example" passes it while still being
    a path on this site, which is the point.
    """
    return next if next.startswith("/admin") else "/admin/"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/admin/"):
    next = safe_next(next)
    if request.session.get("staff_id"):
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", admin_ctx(request, next=next, error=None))


@router.post(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit_dependency(login_rate_limiter)), Depends(require_csrf)],
)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/"),
    db: Session = Depends(get_db),
):
    staff = authenticate(db, email, password)
    if staff is None:
        return templates.TemplateResponse(
            request, "admin/login.html",
            admin_ctx(request, next=next, error="Invalid email or password"),
            status_code=401,
        )
    start_session(request, staff)
    # Never redirect off /admin -- closes the obvious open-redirect hole
    # a crafted `next` value could otherwise exploit.
    return RedirectResponse(url=safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, staff: StaffUser = Depends(require_staff)):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)
