"""An AI read will not pick a venue for you.

Aaron, 2026-09-12: "my actual pattern is checking six dates across two
venues in a row while drafting replies. A response clearly labelled
'hamilton' when I asked about the Entrance is something I'd miss on the
fourth check, because I'm reading the dates, not the label. Then I quote a
free Saturday at the wrong building. Refusing is the right answer there. If
I don't name the venue, I get an error and I ask again. That costs me two
seconds and it can't be misread."

TWO QUESTIONS, KEPT APART, and the distinction is the security one:
  * WHICH VENUES MAY THIS CREDENTIAL SEE -- authorisation, from
    AI_VENUE_SLUG. A request can never widen past it.
  * WHICH VENUE IS THIS CALL ABOUT -- a required argument that narrows
    within it.

These build their OWN client rather than using the shared ai_client
fixture, which supplies a venue automatically. A test of "the venue is
required" written against that fixture would pass because of the fixture.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Space, Venue

TOKEN = "ai-venue-test-token"
LIST_READS = ["/api/ai/pipeline", "/api/ai/bookings", "/api/ai/catalogue"]


@pytest.fixture()
def bare_client(db, hamilton, monkeypatch):
    """No venue is added for you. That is the point."""
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


# --- required, not defaulted ----------------------------------------------


@pytest.mark.parametrize("path", LIST_READS)
def test_a_list_read_without_a_venue_is_refused(bare_client, hamilton, path):
    resp = bare_client.get(path)

    assert resp.status_code == 400, (
        f"{path} answered {resp.status_code} with no venue named; it must refuse"
    )
    assert "venue is required" in resp.text
    assert "hamilton" in resp.text, "the refusal must say what can be named"


def test_availability_without_a_venue_is_refused_before_anything_else(bare_client, hamilton):
    """Availability takes a date too. The venue check must come FIRST, or a
    caller who omits both is told about the date and fixes that, then gets
    told about the venue -- two round trips for one mistake."""
    resp = bare_client.get("/api/ai/availability")

    assert resp.status_code == 400
    assert "venue is required" in resp.text


# What each read needs BESIDES a venue. /api/ai/bookings genuinely requires
# one of ref/email/date -- a venue alone is not a question it can answer --
# so asserting 200 from venue alone would have been asserting the wrong
# thing about a correct refusal.
EXTRA = {
    "/api/ai/pipeline": "",
    "/api/ai/catalogue": "",
    "/api/ai/bookings": f"&date={(dt.date.today() + dt.timedelta(days=5)).isoformat()}",
}


@pytest.mark.parametrize("path", LIST_READS)
def test_naming_the_venue_works(bare_client, hamilton, path):
    """The other half -- a refusal that refused everything would pass every
    test above while making the API useless."""
    resp = bare_client.get(f"{path}?venue={hamilton.slug}{EXTRA[path]}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["venue"] == hamilton.slug


def test_the_answer_is_about_the_venue_that_was_asked_for(
    bare_client, db, hamilton, entrance, monkeypatch
):
    """THE point. Not "an answer with a label" -- an answer ABOUT the venue
    named, which is what makes the label true.

    The credential must permit BOTH here. Without that the second call is
    correctly refused as out of reach, and the test would be measuring the
    authorisation ceiling instead of the argument.
    """
    import datetime as dt

    from app.services.booking import create_booking

    monkeypatch.setattr(settings, "ai_venue_slug", f"{hamilton.slug},{entrance.slug}")

    create_booking(
        db, space_id=entrance.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=5),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="ZZENTRANCEONLY",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()

    ham = bare_client.get(f"/api/ai/bookings?venue={hamilton.slug}&date={(dt.date.today() + dt.timedelta(days=5)).isoformat()}")
    ent = bare_client.get(f"/api/ai/bookings?venue={entrance.slug}&date={(dt.date.today() + dt.timedelta(days=5)).isoformat()}")

    assert "ZZENTRANCEONLY" not in ham.text, "Hamilton's answer contained an Entrance booking"
    assert "ZZENTRANCEONLY" in ent.text, "The Entrance's answer did not contain its own booking"


# --- the authorisation ceiling --------------------------------------------


def test_a_venue_outside_the_credentials_reach_is_refused(bare_client, db, hamilton, entrance):
    """The required argument NARROWS within what the credential permits and
    never widens past it. AI_VENUE_SLUG lists hamilton only here, so naming
    the entrance must fail even though that venue exists."""
    resp = bare_client.get(f"/api/ai/pipeline?venue={entrance.slug}")

    assert resp.status_code == 404
    assert entrance.slug in resp.text


def test_an_unknown_venue_and_a_forbidden_one_are_refused_alike(bare_client, db, hamilton, entrance):
    """Same status and same shape, so the argument cannot be used to
    discover which venues exist but are not readable."""
    forbidden = bare_client.get(f"/api/ai/pipeline?venue={entrance.slug}")
    nonsense = bare_client.get("/api/ai/pipeline?venue=not-a-real-venue")

    assert forbidden.status_code == nonsense.status_code == 404


def test_a_credential_may_list_several_venues(bare_client, db, hamilton, entrance, monkeypatch):
    """Two venues on one credential, which is what Aaron will have. The
    argument then selects between them."""
    monkeypatch.setattr(settings, "ai_venue_slug", f"{hamilton.slug},{entrance.slug}")

    for venue in (hamilton, entrance):
        resp = bare_client.get(f"/api/ai/pipeline?venue={venue.slug}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["venue"] == venue.slug


def test_the_refusal_lists_every_venue_the_credential_can_reach(
    bare_client, db, hamilton, entrance, monkeypatch
):
    """So the answer to "which do I name?" is in the error itself."""
    monkeypatch.setattr(settings, "ai_venue_slug", f"{hamilton.slug},{entrance.slug}")

    resp = bare_client.get("/api/ai/pipeline")

    assert resp.status_code == 400
    assert hamilton.slug in resp.text and entrance.slug in resp.text


# --- by-id reads are a different question ----------------------------------


def test_a_by_id_read_does_not_require_a_venue(bare_client, db, hamilton, loft, contact):
    """A by-id read NAMES one booking, so the only question is whether this
    credential may see it -- the permitted SET, not a requested venue.
    Requiring an argument here would add friction with no safety gain."""
    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 7, 3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="By Id",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="test",
    )
    db.flush()

    resp = bare_client.get(f"/api/ai/bookings/{booking.id}/events")

    assert resp.status_code == 200, resp.text


def test_a_by_id_read_still_refuses_a_booking_outside_the_credential(
    bare_client, db, hamilton, entrance
):
    """And the authorisation check is still there. Resolving the venue FROM
    the booking would have removed it rather than scoped it."""
    from app.services.booking import create_booking

    booking = create_booking(
        db, space_id=entrance.spaces[0].id, contact_id=None, event_date=dt.date(2027, 7, 3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Not Yours",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="test",
    )
    db.flush()

    resp = bare_client.get(f"/api/ai/bookings/{booking.id}/events")

    assert resp.status_code == 404
