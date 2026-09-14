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
    from app.api.admin_auth import router
    from app.venue_scope import venue_scope

    declared = [d.dependency for d in router.dependencies]

    assert venue_scope not in declared, (
        "the auth router resolves a venue; /admin/login is public and must not"
    )


# Routers that have MOVED onto /admin/{venue_slug}/. This list IS the
# rollout: move a router, add it here, and the tests below check the two
# halves that have to stay in step.
# Every admin router, all of them moved. When this equals ALL_STAFF_ROUTERS
# the rollout is done, which is now.
MOVED_ROUTERS = {
    "admin_bookings", "admin_calendar", "admin_dashboard", "admin_drafts",
    "admin_invoices", "admin_reports", "admin_staff", "admin_triage",
}

ALL_STAFF_ROUTERS = (
    "admin_bookings", "admin_calendar", "admin_dashboard", "admin_drafts",
    "admin_invoices", "admin_reports", "admin_staff", "admin_triage",
)


def _router_for(name):
    import importlib

    return importlib.import_module(f"app.api.{name}").router


def test_every_staff_router_takes_its_venue_from_the_path():
    """No router may serve an admin page without knowing its venue, and
    there is now exactly ONE way to know it.

    This used to accept EITHER `venue_scope` (venue from the path) or
    `current_venue` (a hardcoded `filter_by(slug="hamilton")`), because
    during the rollout both existed. The rollout finished and
    `current_venue` was deleted on 2026-09-14 -- it had no callers and a
    docstring still calling itself the seam, so a new router declaring it
    would have passed this very test while serving Hamilton's bookings
    under /admin/entrance/. Nothing would have failed: Hamilton is a real
    venue and its rows are real rows.

    Naming the one mechanism is what makes this test refuse that.
    """
    from app.admin_auth import require_staff
    from app.venue_scope import venue_scope

    missing = []
    for name in ALL_STAFF_ROUTERS:
        declared = [d.dependency for d in _router_for(name).dependencies]
        if require_staff in declared and venue_scope not in declared:
            missing.append(name)

    assert not missing, (
        f"these staff routers do not take their venue from the path: {missing}. "
        "venue_scope is the only mechanism; anything else resolves a venue the "
        "URL did not name."
    )


def test_the_hardcoded_seam_stays_deleted():
    """Named explicitly, because a grep for it is what a future caller
    rebuilding the same thing would do first."""
    from app import admin_auth

    assert not hasattr(admin_auth, "current_venue"), (
        "admin_auth.current_venue is back -- it resolved a hardcoded Hamilton "
        "for whatever page declared it. The venue comes from the path segment "
        "via venue_scope."
    )


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


def test_every_nav_link_is_scoped_to_the_venue(admin_client, hamilton):
    """The rollout is over, so every nav link is on the venue segment.

    This replaces a test that checked each link against a MOVED_SECTIONS set,
    which existed only to make a PARTIAL rollout safe -- a link to a section
    that had not moved yet had to stay legacy, or the nav pointed at a 404.
    With every router moved, that set and its admin_url() helper were
    deleted rather than left standing: scaffolding whose fallback branch
    nothing reaches is scaffolding nobody maintains, and a mutation removing
    a section from it changed nothing observable.
    """
    import re

    resp = admin_client.get(f"/admin/{hamilton.slug}/")
    assert resp.status_code == 200

    nav = re.findall(r'<nav class="primary">(.*?)</nav>', resp.text, re.S)
    assert nav, "the nav did not render"
    links = re.findall(r'href="([^"]+)"', nav[0])
    # The count is pinned so a link silently LOST is caught, not just an
    # unscoped one. Nine since 2026-09-14, when Venue set-up was added.
    assert len(links) == 9, f"expected 9 nav links, got {len(links)}"

    unscoped = [l for l in links if not l.startswith(f"/admin/{hamilton.slug}/")]

    assert not unscoped, f"these nav links are not venue-scoped: {unscoped}"


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
                # No /admin/bookings exemption any more. That exemption was
                # correct while admin_bookings had not moved -- its legacy
                # URL was then the real one -- and became WRONG the moment it
                # did, silently exempting the largest router from the check.
                # A mutation proved it: putting a legacy redirect back into
                # admin_bookings left this test green.
                if target.startswith("/admin/login") or target.startswith("/admin/logout"):
                    continue  # never scoped
                offenders.append(f"app/api/{name}.py:{n} -> {target}")

    assert not offenders, (
        "these moved routers redirect to a legacy path, which loses any query "
        f"string on the way through the compat route: {offenders}"
    )


# --- the compat by-id redirect, which every emailed link depends on --------


def test_a_legacy_booking_link_lands_on_its_own_venue(raw_admin_client, db, hamilton, loft, contact):
    """The reason emails keep the legacy /admin/bookings/{id} path.

    Eight sites in app/services build that URL for the digest, the enquiry
    alert and the BEO review link. They are deliberately NOT rewritten to
    scoped URLs: a link that works out its own venue cannot go stale, cannot
    be wrong when the message is forwarded, and still resolves a year later.
    A baked-in slug fails all three.

    So the compat route must resolve the venue FROM THE BOOKING ROW and land
    on that venue's page -- not on a chooser, and not on a 404.
    """
    import datetime as dt

    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 6, 5),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Emailed Link Target",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()

    # The raw legacy URL, exactly as an email contains it.
    resp = raw_admin_client.get(f"/admin/bookings/{booking.id}", follow_redirects=True)

    assert resp.status_code == 200
    assert f"/admin/{hamilton.slug}/bookings/{booking.id}" in str(resp.url), (
        f"an emailed link did not land on its own venue: {resp.url}"
    )
    assert "Emailed Link Target" in resp.text, "it landed somewhere, but not on the booking"


def test_a_legacy_link_to_a_booking_that_does_not_exist_does_not_404_the_operator(
    raw_admin_client, db, hamilton
):
    """A deleted booking's link should put somebody somewhere useful rather
    than on a dead end -- the same reasoning as the admin error page."""
    missing = "00000000-0000-0000-0000-000000000000"

    resp = raw_admin_client.get(f"/admin/bookings/{missing}", follow_redirects=True)

    assert resp.status_code == 200, "a stale link to a deleted booking dead-ended"


# --- the compat layer's OWN query string ----------------------------------
#
# The fix for "a redirect rebuilt from a literal drops the query string" was
# applied to four in-router redirects. THE COMPAT LAYER BUILDS ITS URL THE
# SAME WAY and was never touched -- and then d3c4919 added the bookings list
# to MOVED_LIST_PATHS, putting every bookings filter behind it.
#
# The test above (test_no_moved_router_redirects_to_its_own_legacy_path) is
# structural: it stops a ROUTER pointing at a legacy path. It says nothing
# about what the legacy path then does with what it was given, which is
# where the bug actually lived.


def test_a_bookmarked_filtered_list_keeps_its_filter(raw_admin_client, db, hamilton):
    """THE one. /admin/bookings?status=enquiry lands on an UNFILTERED list,
    showing exactly the bookings the filter was there to exclude, with
    nothing on the page saying the filter was dropped."""
    resp = raw_admin_client.get(
        "/admin/bookings?status=enquiry&q=smith", follow_redirects=False
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/admin/hamilton/bookings"), location
    assert "status=enquiry" in location, f"the status filter was dropped: {location}"
    assert "q=smith" in location, f"the search term was dropped: {location}"


def test_an_emailed_link_keeps_what_was_on_it(raw_admin_client, db, hamilton, loft, contact):
    """The by-id redirect, which every emailed link goes through.
    ?saved=1 is the confirmation banner and ?tab=documents is which tab
    opens -- both silently lost."""
    import datetime as dt

    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 6, 6),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Query Carrier",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()

    resp = raw_admin_client.get(
        f"/admin/bookings/{booking.id}?saved=1&tab=documents", follow_redirects=False
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "saved=1" in location and "tab=documents" in location, location


def test_a_bare_legacy_url_does_not_gain_a_dangling_question_mark(raw_admin_client, db, hamilton):
    """The other direction. A trailing '?' on every legacy redirect is the
    sort of thing that turns up in an access log and in somebody's
    bookmark."""
    resp = raw_admin_client.get("/admin/bookings", follow_redirects=False)

    assert resp.headers["location"] == "/admin/hamilton/bookings"


def test_the_venue_chooser_carries_the_query_onto_every_choice(
    raw_admin_client, db, hamilton, staff_user
):
    """With two venues the legacy URL renders a chooser instead of
    redirecting, and each choice is a link built the same way -- so it drops
    the filter just as silently, one click later."""
    from decimal import Decimal

    from app.models import Space, Venue

    entrance = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
    )
    db.add(entrance)
    db.flush()
    db.add(Space(
        venue_id=entrance.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()

    resp = raw_admin_client.get("/admin/bookings?status=enquiry", follow_redirects=False)

    assert resp.status_code == 200, "two venues should offer a choice, not redirect"
    # Jinja escapes & to &amp; inside an href, which is correct HTML and what
    # a browser follows -- so accept either spelling rather than pinning one.
    body = resp.text.replace("&amp;", "&")
    assert "/admin/hamilton/bookings?status=enquiry" in body, body[:400]
    assert "/admin/entrance/bookings?status=enquiry" in body
