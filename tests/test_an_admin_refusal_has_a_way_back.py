"""A staff member who hits a refusal gets a page, not raw JSON.

Every admin refusal raises HTTPException, and the default renders it as
{"detail": "..."} on an otherwise blank page. In the desktop app there is
no browser chrome and no Back button, so that is a DEAD END: the only way
out is to close and reopen the app.

Aaron hit it on 2026-09-10 saving an agreed minimum without a reason, on
HAM-20261121-X9CGO. There are 82 other `raise HTTPException` in the admin
routers that would each have done the same thing.

The split is by path and nothing else: /admin gets a page, /api keeps JSON.
That second half is load-bearing -- the AI read API and the MCP both depend
on seeing the real status and detail, which mcp_server/concierge.py says
out loud: "the model should see exactly that and stop, not receive an empty
result it might read as 'nothing found'".
"""

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Contact
from app.services.booking import create_booking


def _booking(db, space, name="Refusal"):
    contact = Contact(name=name, email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 11, 21),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _csrf(client, booking_id):
    return re.search(
        r'name="csrf_token" value="([^"]+)"', client.get(f"/admin/bookings/{booking_id}").text
    ).group(1)


# --- the exact thing Aaron hit -----------------------------------------------


def test_the_agreed_minimum_refusal_is_a_page_with_a_way_back(admin_client, db, loft):
    booking = _booking(db, loft, "Refusal Minimum")
    assert loft.standard_min_adults != 30, "the fixture has to actually differ from the standard"

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={"csrf_token": _csrf(admin_client, booking.id), "agreed_min_adults": 30, "reason": ""},
        follow_redirects=False,
        headers={"referer": f"http://testserver/admin/bookings/{booking.id}"},
    )

    assert resp.status_code == 422, "still a refusal -- the rule is unchanged"
    assert "application/json" not in resp.headers["content-type"], "a staff member got raw JSON"
    assert "a reason is required" in resp.text, "the reason for the refusal is not on the page"
    assert f'href="/admin/bookings/{booking.id}"' in resp.text, "no way back to the booking"
    db.refresh(booking)
    assert booking.agreed_min_adults != 30, "the refusal did not actually refuse"


def test_the_rule_is_about_differing_not_reducing(admin_client, db, loft):
    """The label said "required if reduced" and the rule says "differs from
    the space standard". Raising it above the standard needs a reason too,
    which is exactly the wording that mislead somebody into the dead end."""
    booking = _booking(db, loft, "Refusal Higher")
    higher = loft.standard_min_adults + 25

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={"csrf_token": _csrf(admin_client, booking.id), "agreed_min_adults": higher, "reason": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 422
    page = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "required if reduced" not in page, "the label still says the wrong rule"
    assert "differs from the standard" in page


def test_a_valid_change_still_goes_through(admin_client, db, loft):
    """The handler must cost the ordinary path nothing."""
    booking = _booking(db, loft, "Refusal Valid")

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={
            "csrf_token": _csrf(admin_client, booking.id),
            "agreed_min_adults": 30,
            "reason": "aaron_discretion",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == 30


# --- the split: /admin gets a page, /api keeps JSON ---------------------------


def test_a_404_inside_admin_is_also_a_page(admin_client, db, loft):
    booking = _booking(db, loft, "Refusal 404")
    missing = "00000000-0000-0000-0000-000000000000"

    resp = admin_client.get(f"/admin/bookings/{booking.id}/documents/{missing}/preview")

    assert resp.status_code == 404
    assert "application/json" not in resp.headers["content-type"]
    assert "Not found" in resp.text
    assert 'href="/admin"' in resp.text, "a 404 with no Referer still needs a way out"


def test_the_ai_api_still_answers_json(db, hamilton, monkeypatch):
    """Load-bearing. The MCP passes Concierge's errors through so the model
    sees the real reason and stops; an HTML page would be read as content."""
    monkeypatch.setattr(settings, "ai_api_token", "test-ai-token-do-not-use-in-production")
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": "Bearer test-ai-token-do-not-use-in-production"})
        resp = client.get("/api/ai/bookings/00000000-0000-0000-0000-000000000000/invoices")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]
    assert "detail" in resp.json(), "the AI API stopped returning a machine-readable error"


def test_an_unauthenticated_admin_route_still_redirects_to_login(db, hamilton):
    """NotAuthenticated has its own handler and must keep it -- landing on
    an error page instead of the login form would be a worse dead end than
    the one this fixes."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/admin/bookings", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


# --- the Back link is not an open redirect ------------------------------------


@pytest.mark.parametrize(
    "referer",
    [
        "https://evil.example.com/admin/bookings",   # someone else's host
        "http://testserver/wizard/steal",            # same host, not admin
        "javascript:alert(1)",                       # not a URL at all
        "",
    ],
)
def test_the_back_link_never_leaves_the_admin(admin_client, db, loft, referer):
    """The Referer is chosen by whoever made the request. Putting it into a
    link unvalidated is the open redirect this codebase already had to fix
    once on /admin/login."""
    booking = _booking(db, loft, f"Refusal Referer {abs(hash(referer)) % 1000}")

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={"csrf_token": _csrf(admin_client, booking.id), "agreed_min_adults": 31, "reason": ""},
        follow_redirects=False,
        headers={"referer": referer} if referer else {},
    )

    assert resp.status_code == 422
    assert 'href="/admin"' in resp.text
    assert "evil.example.com" not in resp.text
    assert "javascript:" not in resp.text
    assert "/wizard/steal" not in resp.text
