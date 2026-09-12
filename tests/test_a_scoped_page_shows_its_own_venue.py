"""A page under /admin/{venue_slug}/ must show THAT venue's rows.

The dangerous half-finished state, and the one this file exists to prevent:
the URL and the venue band say one company while the query still resolves
another. A mixed list is visibly wrong -- two companies' bookings in one
table is something you notice. A confidently MISLABELLED list is not: it
looks exactly like a correct page, and every number on it is somebody
else's.

Found 2026-09-12, after the routers moved onto the venue segment but their
`_venue(db)` helpers still read `filter_by(slug="hamilton")`. Five routers,
eight call sites, every one answering Hamilton whatever the URL said.
"""
import ast
import datetime as dt
import pathlib
from decimal import Decimal

import pytest

from app.models import Space, Venue
from app.services.booking import create_booking
from tests.test_admin_shows_its_venue import MOVED_ROUTERS


@pytest.fixture()
def entrance(db, hamilton):
    """A second venue with its own space, so a leak is visible."""
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT", trading_days=[2, 3, 4, 5, 6],
    )
    db.add(venue)
    db.flush()
    space = Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(space)
    db.flush()
    venue.space = space
    return venue


# The calendar renders ONE WEEK and defaults to the current one, so a booking
# dated years out appears on nobody's calendar. The first version of this file
# booked into 2027 and every sentinel assertion passed over an EMPTY page --
# vacuously, while the bug it was written for was live. The week is pinned.
WEEK = dt.date.today() + dt.timedelta(days=2)


def _book(db, space, name):
    return create_booking(
        db, space_id=space.id, contact_id=None, event_date=WEEK,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )


def _calendar(client, venue):
    return client.get(
        f"/admin/{venue.slug}/calendar?week={WEEK.isoformat()}", follow_redirects=True
    )


# Only the calendar carries a name-sentinel today. The dashboard renders
# counts and recent EVENT rows, and triage lists only bookings that need
# triaging -- a plain confirmed booking reaches neither, so a sentinel there
# would pass no matter what the query did. Stated rather than left implicit:
# the structural test at the bottom is what covers those routers, and a
# page-level probe for them needs different fixtures than a booking.


def test_a_second_venues_page_never_shows_the_first_venues_booking(
    admin_client, db, hamilton, loft, entrance
):
    """THE test. A Hamilton booking with an unmistakable name must not appear
    under The Entrance's prefix."""
    _book(db, loft, "ZZHAMILTONSENTINEL Party")
    db.flush()

    # Proof the sentinel is REACHABLE: it must appear on Hamilton's own copy
    # of the same page. Without this the assertion below passes on an empty
    # page and proves nothing -- which is exactly what happened first time.
    own = _calendar(admin_client, hamilton)
    assert "ZZHAMILTONSENTINEL" in own.text, (
        "the sentinel does not appear on Hamilton's own calendar, so this test "
        "cannot detect a leak onto the other venue either"
    )

    resp = _calendar(admin_client, entrance)

    assert resp.status_code == 200
    assert "ZZHAMILTONSENTINEL" not in resp.text, (
        "The Entrance's calendar showed a HAMILTON booking -- the page is "
        "labelled one venue and queried another"
    )


def test_the_band_and_the_rows_agree(admin_client, db, hamilton, loft, entrance):
    """The specific danger. A page that is honestly empty is fine; a page
    that says The Entrance over Hamilton's rows is not."""
    _book(db, loft, "ZZHAMILTONSENTINEL Party")
    db.flush()

    resp = _calendar(admin_client, entrance)

    assert "Meantime The Entrance" in resp.text, "the band did not name the venue in the URL"
    assert "ZZHAMILTONSENTINEL" not in resp.text


def test_each_venues_own_booking_does_appear(admin_client, db, hamilton, loft, entrance):
    """The other half -- a scoping fix that shows nothing would pass every
    assertion above while being useless."""
    _book(db, loft, "ZZHAMONLY Party")
    _book(db, entrance.space, "ZZENTONLY Party")
    db.flush()

    ham = _calendar(admin_client, hamilton).text
    ent = _calendar(admin_client, entrance).text

    assert "ZZHAMONLY" in ham, "Hamilton's own booking vanished from Hamilton's calendar"
    assert "ZZENTONLY" in ent, "The Entrance's own booking never appeared on its calendar"
    assert "ZZENTONLY" not in ham
    assert "ZZHAMONLY" not in ent


# --- the structural half ---------------------------------------------------


def _hardcoded_venue_lookups(source: str) -> list[tuple[int, str]]:
    """Every `...filter_by(slug="literal")` call in a module, by AST.

    AST, not grep. The first version searched the source TEXT and matched the
    DOCSTRING of the very helper that had just been fixed -- which quotes
    what it used to be. A substring search cannot tell code from prose, and
    skipping lines beginning with "#" does not cover a docstring. Walking
    real Call nodes cannot be fooled that way.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "filter_by":
            continue
        for kw in node.keywords:
            if kw.arg == "slug" and isinstance(kw.value, ast.Constant):
                found.append((node.lineno, kw.value.value))
    return found


def test_no_moved_router_still_resolves_a_hardcoded_venue():
    """A moved router that keeps a hardcoded lookup serves a page whose URL
    says one venue and whose rows are another's."""
    offenders = []
    for name in sorted(MOVED_ROUTERS):
        source = pathlib.Path(f"app/api/{name}.py").read_text(encoding="utf-8")
        for lineno, slug in _hardcoded_venue_lookups(source):
            offenders.append(f"app/api/{name}.py:{lineno} filter_by(slug={slug!r})")

    assert not offenders, (
        f"these routers take their venue from the URL but query a hardcoded one: {offenders}"
    )


def test_the_ast_check_can_actually_see_a_hardcoded_lookup():
    """The premise the test above rests on. If the walker stopped finding
    Call nodes it would report clean over everything -- the route-walker
    failure in a different costume.

    The sample mentions the pattern in its docstring AS WELL AS using it,
    because matching both is exactly what broke the grep version.
    """
    sample = "\n".join([
        "def _venue(db):",
        "    'This used to be filter_by(slug=hamilton).'",
        '    return db.query(Venue).filter_by(slug="hamilton").one()',
    ])

    found = _hardcoded_venue_lookups(sample)

    assert [slug for _, slug in found] == ["hamilton"], (
        f"the walker found {found} in a sample that plainly has exactly one"
    )
