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


# --- the drafts page, which had no venue in it at all -----------------------
#
# admin_drafts moved onto /admin/{venue_slug}/ and gained venue_scope, and
# then nothing inside it was scoped: a bare select(EnquiryDraft) for the list
# and a bare db.get() for the review POST. So Hamilton's URL listed The
# Entrance's drafts -- including its clients' own enquiry text -- and could
# mark another venue's draft reviewed.
#
# It was PREDICTED. app/admin_auth.py says, in a comment: "app/api/
# admin_drafts.py contains no mention of a venue at all today, and that is
# exactly how a page ends up outside the scoping." The structural check
# below missed it precisely because there was no hardcoded lookup to find --
# the failure was an ABSENT predicate, not a wrong one.


def _csrf(html: str) -> str:
    """The token, taken from the page the form lives on.

    Without it the POST is refused 422 by require_csrf BEFORE the venue
    check runs -- so a test that omitted it would pass while proving nothing
    about scoping at all.
    """
    import re

    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _draft(db, booking, text):
    from app.models.enquiry_draft import STATUS_GENERATED, EnquiryDraft

    draft = EnquiryDraft(
        booking_id=booking.id, status=STATUS_GENERATED, trigger="enquiry_received",
        draft_text=text,
    )
    db.add(draft)
    db.flush()
    return draft


def test_the_drafts_page_shows_only_its_own_venues_drafts(
    admin_client, db, hamilton, loft, entrance
):
    ham_booking = _book(db, loft, "Ham Draft Booking")
    ent_booking = _book(db, entrance.space, "Ent Draft Booking")
    _draft(db, ham_booking, "ZZHAMDRAFT sentinel body")
    _draft(db, ent_booking, "ZZENTDRAFT sentinel body")
    db.flush()

    own = admin_client.get("/admin/hamilton/drafts", follow_redirects=True)
    assert "ZZHAMDRAFT" in own.text, (
        "the sentinel is not on Hamilton's own drafts page, so this test could "
        "not detect a leak onto the other venue either"
    )

    other = admin_client.get("/admin/entrance/drafts", follow_redirects=True)

    assert other.status_code == 200
    assert "ZZHAMDRAFT" not in other.text, (
        "The Entrance's drafts page listed a HAMILTON draft -- and a draft "
        "carries the client's own words"
    )
    assert "ZZENTDRAFT" in other.text, "it showed nothing at all, which is not the fix"


def test_one_venues_url_cannot_review_another_venues_draft(
    admin_client, db, hamilton, loft, entrance
):
    """Reading the wrong list is bad; WRITING to the wrong venue's row from
    a venue-scoped URL is worse. 404, not 403 -- saying 'forbidden' would
    confirm the draft exists."""
    ent_booking = _book(db, entrance.space, "Ent Draft Booking")
    draft = _draft(db, ent_booking, "ZZENTDRAFT sentinel body")
    db.flush()

    token = _csrf(admin_client.get("/admin/hamilton/drafts", follow_redirects=True).text)

    resp = admin_client.post(
        f"/admin/hamilton/drafts/{draft.id}/review",
        data={"csrf_token": token, "outcome": "discarded", "discard_reason": "not mine to discard"},
        follow_redirects=False,
    )

    assert resp.status_code == 404, resp.status_code
    db.refresh(draft)
    assert draft.outcome is None, "another venue's draft was marked reviewed"
    assert draft.reviewed_by is None


def test_a_venue_can_still_review_its_own_draft(admin_client, db, hamilton, loft, entrance):
    """The other direction, so a check that refused everything could not
    pass the test above on its own."""
    booking = _book(db, loft, "Ham Draft Booking")
    draft = _draft(db, booking, "ZZHAMDRAFT sentinel body")
    db.flush()

    token = _csrf(admin_client.get("/admin/hamilton/drafts", follow_redirects=True).text)

    resp = admin_client.post(
        f"/admin/hamilton/drafts/{draft.id}/review",
        data={"csrf_token": token, "outcome": "discarded", "discard_reason": "wrong tone"},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    db.refresh(draft)
    assert draft.outcome == "discarded"


def test_the_page_says_the_switches_are_not_per_venue(admin_client, db, hamilton):
    """AiSettings is one process-wide row with no venue_id, so turning
    drafting off here turns it off for every venue. That needs a column and
    a decision; until then the page has to SAY so, because a global switch
    inside a venue-scoped URL is the same trap as a global list."""
    body = admin_client.get("/admin/hamilton/drafts", follow_redirects=True).text

    assert "apply to every venue" in body, (
        "the switch block does not say it reaches beyond this venue"
    )


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


def test_every_moved_router_mentions_the_venue_it_is_scoped_to():
    """The check that would have caught admin_drafts, which the one above
    could not.

    That router had no hardcoded `filter_by(slug=...)` to find -- it had no
    venue ANYWHERE. A missing predicate leaves no trace for a check that
    looks for a wrong one. So: a module mounted under /admin/{venue_slug}/
    must at least refer to the venue the URL gave it.

    Deliberately crude. It cannot prove a query is scoped; it can prove a
    router is not ignoring the segment entirely, which is the state five
    routers and then a sixth were found in.
    """
    silent = []
    for name in sorted(MOVED_ROUTERS):
        source = pathlib.Path(f"app/api/{name}.py").read_text(encoding="utf-8")
        stripped = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        if "request.state.venue" not in stripped:
            silent.append(f"app/api/{name}.py")

    assert not silent, (
        "these routers are mounted under /admin/{venue_slug}/ but never read the "
        f"venue the URL named, so every query in them spans every venue: {silent}"
    )
