"""A gate that opens onto step 9, not a note that gets scrolled past.

StaffUser and StaffAppToken have NO venue column. Today that is
correct-by-absence rather than fixed: Aaron is the only login, so a staff
page listing "every staff user" and "this venue's staff users" return the
same rows and nothing is wrong.

The day The Entrance's venue row is created, that stops being true. The
staff page starts listing two companies' people and their live device
tokens, under one company's URL and one company's venue band -- which is the
exact failure this project has now hit twice: a confidently mislabelled
page, indistinguishable from a correct one.

So this is a TEST rather than a line in a document. A note is something you
read if you happen to look; a failing test is something you cannot deploy
past. It goes red at precisely the moment the work becomes necessary, and
its message says what to do.
"""
import pytest
from sqlalchemy import inspect, select

from app.database import Base
from app.models import Venue

SCOPE_ME = ("staff_users", "staff_app_tokens")


def _has_venue_column(table_name: str) -> bool:
    for mapper in Base.registry.mappers:
        if mapper.class_.__tablename__ == table_name:
            return "venue_id" in {c.key for c in mapper.columns}
    raise AssertionError(f"no mapped class for {table_name!r} -- has it been renamed?")


def test_staff_scoping_is_required_once_a_second_venue_exists(db, hamilton):
    """THE gate.

    Passes while there is one venue. Fails the moment there are two and
    staff still carry no venue, which is step 9's morning.
    """
    venues = db.scalars(select(Venue)).all()
    unscoped = [t for t in SCOPE_ME if not _has_venue_column(t)]

    if len(venues) <= 1:
        pytest.skip(
            f"one venue, so staff scoping is not yet observable "
            f"(still unscoped: {unscoped or 'none'})"
        )

    assert not unscoped, (
        f"There are now {len(venues)} venues and {unscoped} still have no venue_id.\n"
        "\n"
        "The staff page lists EVERY staff user and EVERY live device token, under\n"
        "one venue's URL and one venue's band. That is the mislabelled-page failure\n"
        "again: it looks like a correct page for the venue in the address bar.\n"
        "\n"
        "This is step 8 of the venue switch (the floor slab):\n"
        "  * staff_users.venue_id, feeding issue_app_token\n"
        "  * staff_app_tokens.venue_id, backfilled to Hamilton including revoked rows\n"
        "  * a venue predicate on admin_staff's two queries\n"
        "  * Aaron, 2026-09-12: admins stay NULL and get a venue picker at floor\n"
        "    sign-in; floor staff never see it and cannot choose.\n"
    )


def test_the_gate_can_actually_tell(db, hamilton):
    """The premise. If _has_venue_column silently answered True for
    everything -- a rename, a model change -- the gate above would skip or
    pass forever without ever being able to fire.

    Checked against a table that DOES carry venue_id and one that does not,
    so the function is proven to distinguish rather than just to return.
    """
    assert _has_venue_column("bookings") is True, "cannot see a venue_id that exists"
    assert _has_venue_column("staff_users") is False, (
        "staff_users now HAS venue_id -- if step 8 is done, delete this file and "
        "replace it with real scoping tests on admin_staff"
    )


def test_the_gate_names_a_real_table():
    """A gate watching a table that no longer exists is a gate watching
    nothing. _has_venue_column raises rather than returning a default, and
    this proves it."""
    with pytest.raises(AssertionError, match="no mapped class"):
        _has_venue_column("a_table_that_does_not_exist")


def test_the_admin_staff_page_is_honest_about_what_it_lists(db, hamilton):
    """While it is unscoped, it must not CLAIM to be scoped. The page is
    reached under /admin/{venue_slug}/staff and carries the venue band, so a
    reader would reasonably assume the list belongs to that venue."""
    import pathlib

    source = pathlib.Path("app/templates/admin/staff_users.html").read_text(encoding="utf-8")

    assert "every venue" in source.lower() or "all venues" in source.lower(), (
        "app/templates/admin/staff_users.html does not say that the list covers "
        "every venue. It sits under a venue-scoped URL with a venue band, so "
        "without that sentence it reads as this venue's staff."
    )
