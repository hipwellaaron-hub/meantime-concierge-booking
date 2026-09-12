"""The Event Order's AV section follows the screen, not a room's name.

    if booking.space.name != "The Loft"

A Hamilton room name, as a string literal, standing in for a property of
the room. Wrong twice over once there is a second venue: a room at The
Entrance with a screen in it could never have the section, and any room
anywhere that happened to be called "The Loft" inherited it whether or not
there was anything to plug into.

The capability is a column now, which is what it always was --
`spaces.has_screen`, the same shape as `wheelchair_accessible` and
`has_per_head_shortfall_fee` beside it, and for the same reason.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app.models import Space, Venue
from app.services.booking import create_booking
from app.services.document_generation import build_av_block

AV = {"video_slideshow": True, "microphones_for_speeches": False, "notes": None}


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    return venue


def _room(db, venue, name, *, has_screen):
    space = Space(
        venue_id=venue.id, name=name, capacity=80, standard_min_adults=40,
        min_food_spend=Decimal("1000"), is_bookable=True, has_screen=has_screen,
    )
    db.add(space)
    db.flush()
    return space


def _booking_in(db, space):
    booking = create_booking(
        db, space_id=space.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name="AV Test", event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


# --- the capability decides ------------------------------------------------


def test_a_room_at_another_venue_with_a_screen_gets_the_section(db, hamilton, entrance):
    """THE one the name check made impossible. Nothing about this room is
    called The Loft, and it has a screen."""
    space = _room(db, entrance, "Private Bar Function", has_screen=True)

    block = build_av_block(_booking_in(db, space), AV)

    assert block is not None, "a room with a screen was refused the AV section"
    assert block["video_slideshow"] is True


def test_a_room_called_the_loft_with_no_screen_does_not_get_it(db, hamilton, entrance):
    """The other half of the same bug. A second venue naming a room The
    Loft inherited a section about a screen it does not have -- and the
    section tells the client to bring a USB."""
    space = _room(db, entrance, "The Loft", has_screen=False)

    assert build_av_block(_booking_in(db, space), AV) is None


def test_hamiltons_loft_still_gets_the_section(db, hamilton, loft):
    """The screen really is in that room, so the fixture and the backfill
    have to agree. A change that turned the section off everywhere would
    pass both tests above."""
    assert loft.has_screen is True, "the seeded Loft lost its screen"
    assert build_av_block(_booking_in(db, loft), AV) is not None


def test_a_room_with_no_screen_at_hamilton_still_gets_nothing(db, hamilton, mezzanine):
    assert mezzanine.has_screen is False
    assert build_av_block(_booking_in(db, mezzanine), AV) is None


def test_no_av_response_means_no_section_even_with_a_screen(db, hamilton, loft):
    """The second half of the original condition, kept: a room with a
    screen whose client asked for nothing still renders no section rather
    than an empty one."""
    assert build_av_block(_booking_in(db, loft), None) is None


# --- the sweep -------------------------------------------------------------


def test_no_document_rule_is_keyed_on_a_room_name():
    """A room name in a document rule is a capability somebody could not
    express as data. Catches the next one."""
    import ast
    import pathlib

    source = pathlib.Path("app/services/document_generation.py").read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        # `space.name == "..."` / `!=`, in either order.
        if not isinstance(node, ast.Compare):
            continue
        parts = [node.left, *node.comparators]
        names = [
            p for p in parts
            if isinstance(p, ast.Attribute) and p.attr == "name"
            and isinstance(p.value, ast.Attribute) and p.value.attr == "space"
        ]
        literals = [p for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)]
        if names and literals:
            offenders.append(f"line {node.lineno}: space.name compared with {literals[0].value!r}")

    assert not offenders, (
        f"a document rule is keyed on a room's NAME rather than a property of the room: {offenders}"
    )


def test_the_sweep_can_actually_see_one():
    """The premise the sweep rests on. A walker that stopped matching would
    report clean over everything -- the same failure in a new costume."""
    import ast

    sample = "\n".join([
        "def f(booking):",
        "    if booking.space.name != 'The Loft':",
        "        return None",
    ])
    found = []
    for node in ast.walk(ast.parse(sample)):
        if not isinstance(node, ast.Compare):
            continue
        parts = [node.left, *node.comparators]
        names = [
            p for p in parts
            if isinstance(p, ast.Attribute) and p.attr == "name"
            and isinstance(p.value, ast.Attribute) and p.value.attr == "space"
        ]
        literals = [p for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)]
        if names and literals:
            found.append(literals[0].value)

    assert found == ["The Loft"], f"the walker found {found} in a sample that plainly has one"
