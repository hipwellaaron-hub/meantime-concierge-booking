"""A phone shows the venue it was signed into.

Aaron's question, 2026-09-12: "what actually happens when Karly opens her
phone tomorrow? If it keeps working on Hamilton, that's fine. If it silently
breaks, or worse, silently shows the wrong venue once The Entrance exists,
that needs handling."

Both halves are answered here by running them rather than by reasoning:

  * an EXISTING device keeps working, because migration d6b4e9f2a831
    backfilled every token to Hamilton including revoked ones; and
  * a device signed into another venue was showing HAMILTON's bookings,
    because get_staff_by_app_token returns the staff user and DISCARDS the
    token, so the token's venue never reached the request and
    staff_app._venue was still a hardcoded lookup.

The second is the mislabelled-page failure on the surface the team uses
while a function is actually running.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Space, Venue
from app.models.booking import BookingStatus
from app.services import staff_auth
from app.services.booking import change_status, create_booking


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def entrance(db, hamilton):
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


def _floor_at(db, venue, email):
    return staff_auth.create_or_update_staff_user(
        db, email=email, name=f"Floor {email}", password="floorpassword1",
        role="floor", venue=venue,
    )


def _confirmed(db, space, name):
    booking = create_booking(
        db, space_id=space.id, contact_id=None, event_date=dt.date.today() + dt.timedelta(days=3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    db.flush()
    return booking


def _headers(db, staff, venue):
    return {"Authorization": f"Bearer {staff_auth.issue_app_token(db, staff, venue)}"}


# --- Karly's phone tomorrow ------------------------------------------------


def test_an_existing_device_keeps_working(client, db, hamilton, loft):
    """The first half of Aaron's question. A token issued before this work
    was backfilled to Hamilton by the migration, so the phone in somebody's
    apron pocket keeps working and shows Hamilton -- no re-login, no blank
    screen."""
    karly = _floor_at(db, hamilton, "karly@test")
    headers = _headers(db, karly, hamilton)
    _confirmed(db, loft, "ZZHAMILTON Function")

    resp = client.get("/api/staff/bookings", headers=headers)

    assert resp.status_code == 200, "an existing device stopped working"
    assert "ZZHAMILTON" in resp.text


def test_a_device_signed_into_another_venue_does_not_show_hamiltons_bookings(
    client, db, hamilton, loft, entrance
):
    """THE one that matters, and the one that was broken.

    get_staff_by_app_token returns the staff USER and discards the token, so
    the token's venue never reached the request -- and staff_app._venue was
    a hardcoded Hamilton lookup. A phone signed into The Entrance showed
    Hamilton's run sheets, with no way for the person holding it to tell.
    """
    ruby = _floor_at(db, entrance, "ruby@test")
    headers = _headers(db, ruby, entrance)
    _confirmed(db, loft, "ZZHAMILTON Function")
    _confirmed(db, entrance.space, "ZZENTRANCE Function")

    resp = client.get("/api/staff/bookings", headers=headers)

    assert resp.status_code == 200
    assert "ZZHAMILTON" not in resp.text, (
        "a phone signed into The Entrance showed a HAMILTON booking"
    )
    assert "ZZENTRANCE" in resp.text, "it showed nothing at all, which is not the fix"


def test_each_phone_sees_only_its_own_venue(client, db, hamilton, loft, entrance):
    """Both directions, so a fix that shows nothing cannot pass."""
    karly = _floor_at(db, hamilton, "karly2@test")
    ruby = _floor_at(db, entrance, "ruby2@test")
    _confirmed(db, loft, "ZZHAMILTON Function")
    _confirmed(db, entrance.space, "ZZENTRANCE Function")

    ham = client.get("/api/staff/bookings", headers=_headers(db, karly, hamilton)).text
    ent = client.get("/api/staff/bookings", headers=_headers(db, ruby, entrance)).text

    assert "ZZHAMILTON" in ham and "ZZENTRANCE" not in ham
    assert "ZZENTRANCE" in ent and "ZZHAMILTON" not in ent


def test_the_api_tells_the_phone_which_venue_it_is_showing(client, db, hamilton, entrance):
    """So the header can say it. A phone that shows the right data under no
    label is one tap away from being read as the other venue."""
    ruby = _floor_at(db, entrance, "ruby3@test")

    body = client.get("/api/staff/bookings", headers=_headers(db, ruby, entrance)).json()

    assert body.get("venue") == (entrance.trading_name or entrance.name), (
        f"the floor API does not name the venue it is serving: {body.get('venue')!r}"
    )


# --- what the phone is TOLD, so it stops asserting things for itself --------
#
# floor.html used to hardcode "Meantime Hamilton" in its header and grey out
# Mondays and Tuesdays with `dow === 1 || dow === 2`. Both are facts about a
# venue, written into a file that every venue's phone loads. These pin the
# server half; the JavaScript half was verified in a browser.


def test_the_response_carries_the_venues_trading_days(client, db, hamilton):
    """So the calendar can grey out the right days instead of Hamilton's."""
    karly = _floor_at(db, hamilton, "karly.days@test")

    body = client.get("/api/staff/bookings", headers=_headers(db, karly, hamilton)).json()

    assert body["trading_days"] == [2, 3, 4, 5, 6], "Wednesday to Sunday, from the venue row"


def test_a_venue_with_no_recorded_days_sends_null_rather_than_a_guess(
    client, db, hamilton
):
    """NULL means nobody has said. The app greys out NOTHING in that case --
    an honest blank rather than another venue's week asserted on this one's
    calendar."""
    hamilton.trading_days = None
    db.flush()
    karly = _floor_at(db, hamilton, "karly.null@test")

    body = client.get("/api/staff/bookings", headers=_headers(db, karly, hamilton)).json()

    assert body["trading_days"] is None


def test_a_second_venue_sends_its_own_days(client, db, hamilton, entrance):
    """The whole point: two venues, two weeks, one template."""
    entrance.trading_days = [4, 5]  # Friday and Saturday only
    db.flush()
    ruby = _floor_at(db, entrance, "ruby.days@test")

    body = client.get("/api/staff/bookings", headers=_headers(db, ruby, entrance)).json()

    assert body["trading_days"] == [4, 5]
    assert body["venue"] == "Meantime The Entrance"


def test_the_floor_template_asserts_no_venue_facts_of_its_own():
    """The sweep. A hardcoded venue name or trading week in this file is
    correct for exactly one venue and silently wrong for every other."""
    import pathlib
    import re

    source = pathlib.Path("app/templates/floor/floor.html").read_text(encoding="utf-8")
    # Strip comments before searching: the file explains what it used to do.
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    stripped = "\n".join(
        l for l in stripped.splitlines() if not l.strip().startswith("//")
    )

    offenders = []
    if "Meantime Hamilton" in stripped:
        offenders.append("a venue name is written into the template")
    if re.search(r"dow\s*===?\s*[12]", stripped):
        offenders.append("closed days are computed from a literal weekday")

    assert not offenders, offenders
