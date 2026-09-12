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


# Routers that have MOVED onto /admin/{venue_slug}/. This list IS the
# rollout: move a router, add it here, and the tests below check the two
# halves that have to stay in step.
MOVED_ROUTERS = {
    "admin_dashboard", "admin_reports",
    "admin_calendar", "admin_drafts", "admin_invoices", "admin_staff", "admin_triage",
}  # admin_bookings is the one still to move

ALL_STAFF_ROUTERS = (
    "admin_bookings", "admin_calendar", "admin_dashboard", "admin_drafts",
    "admin_invoices", "admin_reports", "admin_staff", "admin_triage",
)


def _router_for(name):
    import importlib

    return importlib.import_module(f"app.api.{name}").router


def test_every_staff_router_resolves_a_venue_one_way_or_the_other():
    """No router may serve an admin page without knowing its venue.

    During the rollout there are two mechanisms: the moved routers use
    `venue_scope` (venue from the path), the rest still use `current_venue`
    (the hardcoded seam). A router with NEITHER has silently opted out of
    the scoping, which is the thing this catches.
    """
    from app.admin_auth import current_venue, require_staff
    from app.venue_scope import venue_scope

    missing = []
    for name in ALL_STAFF_ROUTERS:
        declared = [d.dependency for d in _router_for(name).dependencies]
        if require_staff in declared and not (
            current_venue in declared or venue_scope in declared
        ):
            missing.append(name)

    assert not missing, f"these staff routers resolve no venue at all: {missing}"


def test_a_moved_router_actually_carries_the_path_segment():
    """The two halves that must stay in step. A router listed as moved but
    still mounted at its old prefix would take `venue_slug` as a QUERY
    parameter and 422 on every request."""
    from app.venue_scope import venue_scope

    wrong = []
    for name in MOVED_ROUTERS:
        router = _router_for(name)
        declared = [d.dependency for d in router.dependencies]
        if "{venue_slug}" not in router.prefix or venue_scope not in declared:
            wrong.append(f"{name} (prefix={router.prefix!r})")

    assert not wrong, f"listed as moved but not actually scoped by path: {wrong}"


def test_an_unmoved_router_is_not_left_on_the_old_seam_by_accident():
    """The reverse. A router that grew the path segment but was not added to
    MOVED_ROUTERS would not get its compat redirect, so its old URL would
    404."""
    unlisted = [
        name for name in ALL_STAFF_ROUTERS
        if name not in MOVED_ROUTERS and "{venue_slug}" in _router_for(name).prefix
    ]

    assert not unlisted, (
        f"{unlisted} carry the venue segment but are not in MOVED_ROUTERS -- "
        "add them, and add their compat entry in app/api/admin_compat.py"
    )


def test_no_compat_route_shadows_a_live_one():
    """THE incremental-rollout trap, and it is silent.

    The compat router is registered BEFORE the venue-scoped routers so it
    owns the literal legacy paths. That means a compat entry for a router
    that has NOT moved yet sits in front of that router's own route and
    shadows it -- redirecting to a venue-scoped URL that does not exist yet,
    so a page that worked a minute ago 404s.

    Reproduced before this test was written: adding all seven legacy list
    paths while only two routers had moved broke /admin/bookings,
    /admin/calendar, /admin/invoices, /admin/triage, /admin/drafts and
    /admin/staff at once.
    """
    from app.api.admin_compat import MOVED_LIST_PATHS, router as compat

    compat_paths = {r.path for r in compat.routes if hasattr(r, "path")}

    live_paths = set()
    for name in ALL_STAFF_ROUTERS:
        router = _router_for(name)
        if "{venue_slug}" in router.prefix:
            continue  # moved; its old path is free for compat to claim
        for route in router.routes:
            # route.path ALREADY carries the router's prefix. Adding it again
            # produced "/admin/bookings/admin/bookings/..." -- which matches
            # nothing, so this test passed over a real shadowing. Found by
            # the mutation check, not by reading it.
            live_paths.add(route.path.rstrip("/") or "/")

    shadowed = sorted({p.rstrip("/") or "/" for p in compat_paths} & live_paths)

    assert not shadowed, (
        f"these compat routes shadow a router that has NOT moved: {shadowed}. "
        f"Remove them from MOVED_LIST_PATHS until that router moves. "
        f"(currently listed: {[p for p, _ in MOVED_LIST_PATHS]})"
    )


# --- the nav during a partial rollout ---------------------------------------


def test_the_nav_never_points_at_a_section_that_has_not_moved(admin_client, hamilton):
    """The trap the incremental rollout sets.

    On a SCOPED page, building every nav link from the venue base would point
    at /admin/hamilton/bookings before that router moved -- a 404 reached by
    clicking the main navigation, on a page that worked. admin_url() decides
    per link instead.

    Reproduced before this test existed: the pilot's nav linked all eight
    sections to the scoped base while only two had moved.
    """
    from app.venue_scope import MOVED_SECTIONS

    resp = admin_client.get(f"/admin/{hamilton.slug}/")
    assert resp.status_code == 200

    import re

    hrefs = re.findall(r'<nav class="primary">(.*?)</nav>', resp.text, re.S)
    assert hrefs, "the nav did not render"
    links = re.findall(r'href="([^"]+)"', hrefs[0])
    assert len(links) == 8, f"expected 8 nav links, got {len(links)}"

    for link in links:
        section = link.replace(f"/admin/{hamilton.slug}", "").replace("/admin", "")
        section = "" if section in ("", "/") else section
        if link.startswith(f"/admin/{hamilton.slug}"):
            assert section in MOVED_SECTIONS, (
                f"{link} points at the venue base but {section!r} has not moved"
            )
        else:
            assert section not in MOVED_SECTIONS, (
                f"{link} uses the legacy path but {section!r} HAS moved -- it should be scoped"
            )


def test_every_nav_link_actually_resolves(admin_client, hamilton):
    """The check that makes the one above mean something: follow each link
    and confirm it is not a 404. A nav that points somewhere consistent but
    wrong would pass the test above and fail a person."""
    import re

    resp = admin_client.get(f"/admin/{hamilton.slug}/")
    nav = re.findall(r'<nav class="primary">(.*?)</nav>', resp.text, re.S)[0]

    broken = []
    for link in re.findall(r'href="([^"]+)"', nav):
        r = admin_client.get(link, follow_redirects=True)
        if r.status_code >= 400:
            broken.append(f"{link} -> {r.status_code}")

    assert not broken, f"nav links that do not resolve: {broken}"


def test_a_venue_slug_cannot_shadow_an_admin_path(db, hamilton, staff_user):
    """A venue slugged "bookings" would make /admin/bookings/... ambiguous
    against the legacy route of the same name, and which one won would
    depend on router registration order.

    Checked when the scope is RESOLVED, not only when a venue is created,
    because a venue row can arrive from a migration, a seed, or by hand.
    """
    import pytest
    from fastapi import HTTPException
    from starlette.datastructures import State

    from app.venue_scope import RESERVED_SLUGS, venue_scope

    assert "bookings" in RESERVED_SLUGS

    hamilton.slug = "bookings"
    db.flush()

    class _Req:
        path_params = {"venue_slug": "bookings"}
        state = State()

    with pytest.raises(HTTPException) as exc:
        venue_scope(_Req(), "bookings", db=db, staff=staff_user)

    assert exc.value.status_code == 404


def test_a_venue_slug_that_did_not_come_from_the_path_is_refused(db, hamilton, staff_user):
    """The query-string escape. A route registered WITHOUT the segment takes
    venue_slug as a QUERY parameter -- verified: such a route answers 422
    normally, but 200 when the parameter is supplied. So a scoped page could
    be reached by a route nobody scoped, via a URL anyone can type.

    Refusing a value that did not arrive in request.path_params closes it.
    """
    import pytest
    from fastapi import HTTPException
    from starlette.datastructures import State

    from app.venue_scope import venue_scope

    class _QueryOnly:
        path_params = {}          # nothing in the path
        state = State()

    with pytest.raises(HTTPException) as exc:
        venue_scope(_QueryOnly(), hamilton.slug, db=db, staff=staff_user)

    assert exc.value.status_code == 404


def test_no_moved_router_redirects_to_its_own_legacy_path():
    """A moved router must send the browser to its SCOPED url, never the
    legacy one.

    The legacy path belongs to the compat route now, and that route rebuilds
    its destination from a fixed literal -- so it DROPS THE QUERY STRING.
    `/admin/staff?outcome=created` became `/admin/hamilton/staff`, and the
    confirmation banner after adding a staff account silently stopped
    appearing.

    I missed this twice by hand: once in the first sweep, which only matched
    single-line redirects, and again in the second, which found a third and a
    fourth. Hence a test rather than another read-through.

    /admin/bookings/... is allowed: that router has NOT moved, so its legacy
    URL is still its real one.
    """
    import pathlib
    import re

    offenders = []
    for name in MOVED_ROUTERS:
        source = pathlib.Path(f"app/api/{name}.py").read_text(encoding="utf-8")
        section = name.replace("admin_", "")
        for n, line in enumerate(source.splitlines(), 1):
            for match in re.finditer(r'url=f?"(/admin/[^"]*)"', line):
                target = match.group(1)
                if target.startswith("/admin/bookings"):
                    continue  # not moved yet; still the real URL
                if target.startswith("/admin/login") or target.startswith("/admin/logout"):
                    continue  # never scoped
                offenders.append(f"app/api/{name}.py:{n} -> {target}")

    assert not offenders, (
        "these moved routers redirect to a legacy path, which loses any query "
        f"string on the way through the compat route: {offenders}"
    )
