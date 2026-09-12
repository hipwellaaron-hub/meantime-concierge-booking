"""The public enquiry form is served, and filed, per venue.

This is the one page a stranger reaches before they have spoken to anybody,
and it was the last hardcoded Hamilton on a client-facing surface: `_venue()`
returned Hamilton by slug no matter who asked, so a second venue's form would
have filed its enquiries against the first and shown a thank-you page that
looked entirely normal.

TWO SEPARATE HAZARDS ARE PINNED HERE.

  1. THE QUERY STRING ON THE REDIRECT. Every live ad points at bare
     /enquire with `utm_*` and `gclid` appended, and enquiry.html reads
     those out of `window.location.search` to build the attribution
     bundle. A redirect that rebuilds its destination from a literal drops
     them, the enquiry records as organic, and nothing errors anywhere.
     That is the same shape as the four admin redirects that lost a week
     filter and a staff banner earlier in this work -- except this one is
     the number Aaron reads to decide ad spend.

  2. WHICH VENUE THE ENQUIRY IS FILED AGAINST. Asserted in both
     directions, because a fix that filed everything at The Entrance would
     satisfy a one-way test just as well as the bug did.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Space, Venue
from app.seed import UNASSIGNED_SPACE_NAME


def _payload(**overrides):
    payload = dict(
        first_name="Sam",
        last_name="Reyes",
        email="sam.reyes@example.com",
        phone="0400111222",
        event_name="Reyes Engagement",
        event_date="2027-03-20",
        dates_flexible="false",
        event_type="Engagement",
        attendee_count=60,
        proposed_time_slot="Saturday evening",
        comments="",
    )
    payload.update(overrides)
    return payload


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def entrance(db, hamilton):
    """A whole second venue, spaces and all -- including the one that holds
    an enquiry before anybody has decided which room it belongs in."""
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT", trading_days=[2, 3, 4, 5, 6],
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


# --- 1. the redirect, and what it must carry -------------------------------


def test_bare_enquire_still_reaches_a_form(client):
    """Every ad, every button on the marketing site and every link already
    sent points here. It has to keep working.

    302, not 301 (Aaron, 2026-09-12). An ad click follows either, but a 301
    is cached hard and permanently -- so a later decision to make bare
    /enquire a venue CHOOSER would never reach anybody who had loaded it
    before. The status is asserted, not just the destination, because that
    is the whole difference."""
    resp = client.get("/enquire")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/enquire/hamilton"


def test_the_redirect_keeps_the_campaign_parameters(client):
    """THE one. `utm_*` and `gclid` ride on this URL from a paid click and
    the form reads them client-side; a destination rebuilt from a literal
    arrives bare, the bundle comes back empty and the enquiry is recorded
    as organic. No error, no exception, just a wrong number in the table
    that decides where the ad money goes."""
    query = "utm_source=google&utm_medium=cpc&utm_campaign=xmas26&gclid=ZZTESTGCLID"

    resp = client.get(f"/enquire?{query}")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("/enquire/hamilton?"), location
    for param in ("utm_source=google", "utm_medium=cpc", "utm_campaign=xmas26", "gclid=ZZTESTGCLID"):
        assert param in location, f"{param} was dropped by the redirect: {location}"


def test_a_redirect_with_no_query_does_not_invent_one(client):
    """A bare '?' on the end is the sort of thing that shows up in an ad
    platform's URL check and gets a campaign disapproved."""
    assert client.get("/enquire").headers["location"] == "/enquire/hamilton"


# --- 2. the form knows which venue it is -----------------------------------


def test_each_venues_form_names_that_venue(client, db, hamilton, entrance):
    """Both directions. The page renders either way; only the label differs,
    which is precisely why nobody would notice it being wrong."""
    ham = client.get("/enquire/hamilton")
    ent = client.get("/enquire/entrance")

    assert ham.status_code == 200 and ent.status_code == 200
    assert "Meantime Hamilton" in ham.text
    assert "Meantime The Entrance" not in ham.text
    assert "Meantime The Entrance" in ent.text
    assert "Meantime Hamilton" not in ent.text


def test_the_form_posts_back_to_its_own_venue(client, db, hamilton, entrance):
    """Serving the right form and posting it to the wrong venue would leave
    the page correct and the booking wrong -- the worst version of this."""
    assert 'action="/enquire/entrance"' in client.get("/enquire/entrance").text
    assert 'action="/enquire/hamilton"' in client.get("/enquire/hamilton").text


def test_an_unknown_venue_is_a_404_and_not_hamiltons_form(client, db, hamilton):
    """A typo'd slug in an ad -- /enquire/entrace -- must not quietly serve
    Hamilton's form under The Entrance's URL."""
    resp = client.get("/enquire/entrace")

    assert resp.status_code == 404
    assert "Meantime Hamilton" not in resp.text


def test_the_internal_venue_label_is_never_printed(client, db, hamilton):
    """`venues.name` is the internal label ("Hamilton") and `trading_name`
    is the client-facing one; the model says never to print the first. The
    gold eyebrow under the heading used to print exactly that."""
    body = client.get("/enquire/hamilton").text

    assert "Meantime Hamilton" in body, "the client-facing name must still be there"
    assert ">Hamilton<" not in body, "the internal venue label reached a public page"


# --- 3. where the enquiry is actually filed --------------------------------


def test_an_enquiry_is_filed_against_the_venue_whose_form_it_came_from(
    client, db, hamilton, unassigned_space, entrance
):
    resp = client.post("/enquire/entrance", data=_payload(event_name="ZZENTRANCE Enquiry"))

    assert resp.status_code == 303, resp.text
    booking = db.query(Booking).filter_by(event_name="ZZENTRANCE Enquiry").one()
    assert booking.venue_id == entrance.id, "an Entrance enquiry was filed at another venue"
    assert booking.space.venue_id == entrance.id


def test_hamiltons_form_still_files_at_hamilton(
    client, db, hamilton, unassigned_space, entrance
):
    """The other direction, so a change that sent everything to the newest
    venue could not pass the test above on its own."""
    resp = client.post("/enquire/hamilton", data=_payload(event_name="ZZHAMILTON Enquiry"))

    assert resp.status_code == 303, resp.text
    booking = db.query(Booking).filter_by(event_name="ZZHAMILTON Enquiry").one()
    assert booking.venue_id == hamilton.id


def test_the_legacy_post_still_works_and_is_hamiltons(client, db, hamilton, unassigned_space):
    """A form page loaded before this deploy, or left open in a tab across
    it, still posts to /enquiries -- and that page was Hamilton's."""
    resp = client.post("/enquiries", data=_payload(event_name="ZZLEGACY Enquiry"))

    assert resp.status_code == 303, resp.text
    booking = db.query(Booking).filter_by(event_name="ZZLEGACY Enquiry").one()
    assert booking.venue_id == hamilton.id


def test_posting_to_an_unknown_venue_creates_nothing(client, db, hamilton, unassigned_space):
    """Refusing is only right if it also refuses to write. A 404 that had
    already filed the booking somewhere would be worse than no 404."""
    before = db.query(Booking).count()

    resp = client.post("/enquire/nowhere", data=_payload(event_name="ZZNOWHERE Enquiry"))

    assert resp.status_code == 404
    assert db.query(Booking).count() == before


# --- 4. the thank-you page -------------------------------------------------


def test_the_thank_you_page_names_the_venue_enquired_at(
    db, hamilton, unassigned_space, entrance
):
    """The receipt a person screenshots. "Which building did I just enquire
    at?" has to be answerable from it once there are two."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=True)
        resp = client.post("/enquire/entrance", data=_payload(event_name="ZZTHANKS Enquiry"))

        assert resp.status_code == 200, resp.text
        assert "Meantime The Entrance" in resp.text
        assert "Meantime Hamilton" not in resp.text
    finally:
        app.dependency_overrides.clear()


# --- 5. the gate for step 9 ------------------------------------------------


def test_a_venue_with_no_triage_space_refuses_rather_than_filing_elsewhere(
    client, db, hamilton, unassigned_space
):
    """THE STEP 9 GATE, and it carries its own instruction.

    An enquiry lands in the venue's "Unassigned (pending triage)" space
    until somebody picks a room. `get_unassigned_space_id` looks that up
    with `scalar_one()` SCOPED TO THE VENUE, so a venue row created without
    one cannot borrow Hamilton's.

    When The Entrance's row goes in (step 9) it needs its spaces created
    with it -- `HAMILTON_SPACES` in app/seed.py is Hamilton's list, not a
    template that runs for every venue. Until then this proves the failure
    is a loud one: a refusal, not an enquiry quietly filed at Hamilton
    under The Entrance's name.
    """
    from sqlalchemy.exc import NoResultFound

    half_built = Venue(
        name="Half Built", slug="half-built", trading_name="Meantime Half Built",
        reference_prefix="HBX",
    )
    db.add(half_built)
    db.flush()

    assert client.get("/enquire/half-built").status_code == 200, (
        "the form itself renders -- the gap only shows on submit"
    )

    before = db.query(Booking).count()
    with pytest.raises(NoResultFound):
        client.post("/enquire/half-built", data=_payload(event_name="ZZHALFBUILT Enquiry"))

    assert db.query(Booking).count() == before, (
        "an enquiry to a half-built venue was filed somewhere anyway"
    )


# --- 6. the sweep ----------------------------------------------------------


def test_neither_public_template_asserts_a_venue_of_its_own():
    """A venue name typed into a template is correct for exactly one venue
    and silently wrong for every other. Both of these had one."""
    import pathlib

    offenders = []
    for name in ("enquiry.html", "enquiry_submitted.html"):
        source = pathlib.Path(f"app/templates/{name}").read_text(encoding="utf-8")
        # Strip Jinja comments: they explain what the file used to do.
        stripped = source
        while "{#" in stripped and "#}" in stripped:
            start = stripped.index("{#")
            end = stripped.index("#}", start) + 2
            stripped = stripped[:start] + stripped[end:]
        if "Meantime Hamilton" in stripped:
            offenders.append(f"{name} names a venue")

    assert not offenders, offenders
