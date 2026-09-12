"""An ad conversion is credited to the venue the lead actually came from.

`_tracking_conversion.html` had `venue: 'hamilton'` written into the GA4
event, on a snippet that fires for every venue's thank-you page.

The SERVER-side copy was already fixed, and the comment on that fix says
exactly what is at stake: "revenue attributed to the wrong business, in the
numbers used to decide where to spend". But the browser copy is the PRIMARY
path -- the server GA4 send exists only as a delayed fallback for a tag that
was blocked -- so in the ordinary case the hardcoded value was the only one
GA4 ever received.

Two companies, one GA4 property, and every Nice Try Events lead arriving
labelled as a Meantime Pty Ltd lead.
"""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Space, Venue
from app.services import conversions
from app.templating import templates

GA4 = "G-TESTONLY1"


def _payload(**overrides):
    payload = dict(
        first_name="Robin", last_name="Vale", email="robin.vale@example.com",
        phone="0400333444", event_name="Vale Birthday", event_date="2027-05-08",
        dates_flexible="false", event_type="Birthday", attendee_count=55,
        proposed_time_slot="Friday evening", comments="",
    )
    payload.update(overrides)
    return payload


@pytest.fixture()
def tags_on(monkeypatch):
    monkeypatch.setitem(templates.env.globals, "ga4_measurement_id", GA4)


@pytest.fixture()
def client(db, hamilton, unassigned_space, tags_on):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, follow_redirects=True)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def entrance(db, hamilton):
    from app.seed import UNASSIGNED_SPACE_NAME

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
    db.add(Space(
        venue_id=venue.id, name=UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    db.flush()
    return venue


def test_an_entrance_enquiry_is_not_credited_to_hamilton(client, db, hamilton, entrance):
    """THE one. The page renders, the tag fires, the conversion is counted --
    against the wrong company."""
    resp = client.post("/enquire/entrance", data=_payload(event_name="ZZENT Conversion"))

    assert resp.status_code == 200, resp.text
    assert f"gtag/js?id={GA4}" in resp.text, "the tag did not render, so this proves nothing"
    assert "venue: 'hamilton'" not in resp.text
    assert '"entrance"' in resp.text, "the conversion did not carry The Entrance's slug"


def test_a_hamilton_enquiry_still_reports_hamilton(client, db, hamilton, entrance):
    """The other direction, so a change that reported nothing, or reported
    the newest venue, could not pass the test above on its own."""
    resp = client.post("/enquire/hamilton", data=_payload(event_name="ZZHAM Conversion"))

    assert resp.status_code == 200, resp.text
    assert '"hamilton"' in resp.text
    assert '"entrance"' not in resp.text


def test_the_browser_and_the_server_agree_on_the_venue(client, db, hamilton, entrance):
    """They are two copies of one conversion, deduplicated by Meta and GA4 on
    the event id. Two different venue labels on one event is a number nobody
    can reconcile afterwards."""
    resp = client.post("/enquire/entrance", data=_payload(event_name="ZZAGREE Conversion"))
    assert resp.status_code == 200

    booking = db.query(Booking).filter_by(event_name="ZZAGREE Conversion").one()
    server_side = conversions.venue_slug_for(booking)

    assert server_side == "entrance"
    assert f'"{server_side}"' in resp.text, (
        "the browser tag and the server-side copy report different venues"
    )


def test_no_venue_is_written_into_the_conversion_template():
    """The sweep. A venue slug in this file is right for one venue and
    silently wrong for every other, on the number that decides ad spend."""
    import pathlib

    source = pathlib.Path("app/templates/_tracking_conversion.html").read_text(encoding="utf-8")
    stripped = source
    while "{#" in stripped and "#}" in stripped:
        start = stripped.index("{#")
        stripped = stripped[:start] + stripped[stripped.index("#}", start) + 2:]
    stripped = "\n".join(
        line for line in stripped.splitlines() if "//" not in line or "venue:" in line
    )

    assert "'hamilton'" not in stripped and '"hamilton"' not in stripped, (
        "a venue slug is written into the conversion tag"
    )
