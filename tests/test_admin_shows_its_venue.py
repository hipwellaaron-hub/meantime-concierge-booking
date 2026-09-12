"""Every admin page says which venue you are looking at.

The failure a venue switch actually has is not a wrong query -- the venue
predicates and the composite FK handle that. It is a CORRECT page read as
the other one: an operator working two venues on a Saturday night, acting on
Hamilton's booking list believing it is The Entrance's.

A band that appears only on the pages that felt risky is one nobody learns to
read, so it is on all of them, in the same place, always.
"""
import pytest

ADMIN_PAGES = [
    "/admin/",
    "/admin/bookings",
    "/admin/calendar",
    "/admin/invoices",
    "/admin/triage",
    "/admin/drafts",
    "/admin/reports/attribution",
    "/admin/staff",
]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_every_admin_page_names_its_venue(admin_client, hamilton, path):
    resp = admin_client.get(path)

    assert resp.status_code == 200, f"{path} did not render"
    assert 'class="venue-band"' in resp.text, f"{path} does not say which venue it is about"
    assert hamilton.trading_name in resp.text


def test_the_band_shows_the_trading_name_not_the_internal_label(admin_client, db, hamilton):
    """`name` is the internal label ("Hamilton"); `trading_name` is what a
    person reads ("Meantime Hamilton"). The model has said "never print
    `name`" since the first import."""
    hamilton.trading_name = "Meantime Somewhere Else"
    db.flush()

    text = admin_client.get("/admin/").text

    assert "Meantime Somewhere Else" in text


def test_a_router_that_never_mentions_a_venue_still_gets_one(admin_client, hamilton):
    """The reason it is a ROUTER-level dependency rather than 17 per-route
    arguments. app/api/admin_drafts.py contains no mention of a venue at all
    -- grep it -- and that is exactly how a page ends up outside the
    scoping. It gets the band without anyone remembering."""
    resp = admin_client.get("/admin/drafts")

    assert resp.status_code == 200
    assert 'class="venue-band"' in resp.text


def test_the_public_login_page_does_not_name_the_venue(db, hamilton):
    """A public login screen must not tell an anonymous visitor which
    companies operate here.

    Two things hold this, and only one of them is load-bearing:
      * admin/login.html is a STANDALONE template -- it does not extend
        _base.html -- so it has no band to render regardless. That is why an
        assertion about `venue-band` here proves nothing, and an earlier
        version of this test passed with a venue dependency added to the
        auth router.
      * The auth router carries NO venue dependency, which is the thing that
        could actually change. test_the_auth_router_declares_no_venue_scope
        below is the one that fails if somebody adds it.
    """
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/admin/login")

        assert resp.status_code == 200
        assert hamilton.trading_name not in resp.text
        assert hamilton.name not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_the_auth_router_declares_no_venue_scope():
    """THE structural one. Nobody is authenticated on /admin/login, so there
    is no venue to be "in" -- resolving one there would mean a public route
    running a database lookup for a venue it must not then mention.

    Asserted against the router's declared dependencies rather than the
    rendered page, because the page cannot show a band either way (its
    template is standalone) and so cannot tell us whether this rule still
    holds."""
    from app.admin_auth import current_venue
    from app.api.admin_auth import router

    declared = [d.dependency for d in router.dependencies]

    assert current_venue not in declared, (
        "the auth router resolves a venue; /admin/login is public and must not"
    )


def test_every_staff_router_DOES_declare_venue_scope():
    """The other half. A router that requires staff must resolve a venue, or
    its pages render with no band and silently opt out of the scoping."""
    import importlib

    from app.admin_auth import current_venue, require_staff

    missing = []
    for name in ("admin_bookings", "admin_calendar", "admin_dashboard", "admin_drafts",
                 "admin_invoices", "admin_reports", "admin_staff", "admin_triage"):
        router = importlib.import_module(f"app.api.{name}").router
        declared = [d.dependency for d in router.dependencies]
        if require_staff in declared and current_venue not in declared:
            missing.append(name)

    assert not missing, f"these staff routers resolve no venue: {missing}"
