"""A venue row with every column filled and no spaces is a 500 on its first enquiry.

THE FAILURE, proven by driving it rather than read off the code: a venue
whose fourteen client-facing columns are all filled serves
`GET /enquire/{slug}` as a normal 200 page. The client fills it in. The
POST returns 500. There is no booking, no notification to staff, and
nothing in the digest -- the lead is gone and nobody knows it existed.

`enquiry_classification.create_enquiry_booking` files every public enquiry
against the venue's "Unassigned (pending triage)" space and reaches it
through `ivvy_import.get_unassigned_space_id`, which is a `scalar_one()`.

This is the shape of the first day of a new venue: Aaron types the venue
row by hand, the marketing site points at `/enquire/entrance`, and the
spaces are a separate job. Between the two the venue looks live.

The fix is not to invent a space. It is to say so -- on /healthz and in the
20:30 digest, beside the unfilled columns, off the same check. A venue
nobody has finished setting up should fail loudly, not half-work.

EVERY PROBE HERE BUILDS THE VENUE COMPLETE AND THEN TAKES ONE THING AWAY.
A bare Venue() is missing fourteen columns as well, so a probe built from
one would go red for a reason that has nothing to do with spaces.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import seed
from app.database import get_db
from app.main import app
from app.models import Space, Venue
from app.services import digest, venue_readiness


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
def entrance(db, hamilton):
    """Complete: every client-facing column copied from Hamilton, its own
    reference prefix, a bookable room and the triage space. Each probe below
    removes exactly one thing."""
    venue = Venue(name="Meantime The Entrance", slug="entrance")
    for column in seed.CLIENT_FACING_COLUMNS:
        setattr(venue, column, getattr(hamilton, column))
    venue.reference_prefix = "ENT"
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.add(Space(
        venue_id=venue.id, name=seed.UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    db.flush()
    return venue


@pytest.fixture()
def public_client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def _spaces(db, venue):
    return list(db.scalars(select(Space).where(Space.venue_id == venue.id)).all())


def _drop_space(db, venue, name):
    for space in _spaces(db, venue):
        if space.name == name:
            db.delete(space)
    db.flush()


# --- the failure itself -------------------------------------------------


def test_a_complete_venue_takes_its_enquiry(public_client, db, entrance):
    """The positive control, and it does real work: without it every probe
    below could be passing on an enquiry route that was broken for some
    unrelated reason."""
    assert venue_readiness.check(db, entrance).is_ready

    r = public_client.post("/enquire/entrance", data=_payload())

    assert r.status_code < 400, f"a fully set-up venue could not take an enquiry: {r.status_code}"


def test_no_triage_space_means_a_200_form_and_a_500_submit(public_client, db, entrance):
    """THE one. Both halves asserted, because the 200 is what makes it
    dangerous -- a form that refused to render would at least be visible."""
    _drop_space(db, entrance, seed.UNASSIGNED_SPACE_NAME)

    assert public_client.get("/enquire/entrance").status_code == 200, (
        "the form no longer renders -- the probe below is about the submit"
    )
    r = public_client.post("/enquire/entrance", data=_payload())

    assert r.status_code == 500, (
        f"expected the lead-losing 500; got {r.status_code}. If this has been "
        "fixed at the source, this probe should assert the new behaviour "
        "rather than be deleted."
    )


# --- so the check has to see it ----------------------------------------


def test_the_missing_triage_space_is_a_gap(db, entrance):
    _drop_space(db, entrance, seed.UNASSIGNED_SPACE_NAME)

    readiness = venue_readiness.check(db, entrance)

    assert not readiness.is_ready
    assert seed.UNASSIGNED_SPACE_NAME in readiness.gaps
    assert seed.UNASSIGNED_SPACE_NAME in readiness.blocking, (
        "a 500 on the enquiry form is listed as if it printed blank on a document"
    )

    # And the other half of the discriminator, without which "everything is
    # blocking" passes -- which is exactly what it did on the first probe run.
    entrance.abn = None
    db.flush()
    widened = venue_readiness.check(db, entrance)
    assert "abn" in widened.gaps
    assert "abn" not in widened.blocking, (
        "an unfilled ABN is called blocking -- it prints blank, which is bad "
        "differently, and calling everything blocking makes the word useless"
    )


def test_no_bookable_space_is_its_own_gap(db, entrance):
    """Separate from the triage space, because they are separate failures: a
    venue can have the triage space and no room to sell, which takes the
    enquiry fine and can never fulfil it."""
    _drop_space(db, entrance, "Private Bar Function")

    readiness = venue_readiness.check(db, entrance)

    assert "a bookable space" in readiness.gaps
    assert seed.UNASSIGNED_SPACE_NAME not in readiness.gaps, (
        "removing the sellable room reported the triage space missing too -- "
        "the two are not being distinguished"
    )


def test_an_unbookable_room_does_not_count_as_one(db, entrance):
    """is_bookable, not "a Space row exists". The triage space is itself a
    Space and is deliberately never bookable, so a check counting rows would
    read every venue as having somewhere to sell."""
    for space in _spaces(db, entrance):
        space.is_bookable = False
    db.flush()

    assert "a bookable space" in venue_readiness.check(db, entrance).gaps


def test_healthz_folds_the_space_gaps_in(db, entrance):
    _drop_space(db, entrance, seed.UNASSIGNED_SPACE_NAME)

    from fastapi.testclient import TestClient as _TC

    app.dependency_overrides[get_db] = lambda: db
    try:
        body = _TC(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()

    assert body["checks"]["venues_ready"] is False
    assert body["status"] == "degraded"


def test_the_digest_says_what_the_missing_space_costs(db, entrance):
    """Named, with the consequence, because "Unassigned (pending triage)" in
    a flat list reads as an internal detail rather than as the reason the
    enquiry form is losing leads."""
    _drop_space(db, entrance, seed.UNASSIGNED_SPACE_NAME)

    content = digest.build_digest(db, entrance)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert seed.UNASSIGNED_SPACE_NAME in body
    assert "500" in body and "lead is lost" in body, (
        "the digest names the missing space without saying what it costs"
    )


def test_a_blocking_gap_outranks_a_blank_one_in_the_email(db, entrance):
    """Both kinds at once, which is the realistic first morning of a new
    venue. The 500 has to be the line read first."""
    _drop_space(db, entrance, seed.UNASSIGNED_SPACE_NAME)
    entrance.abn = None
    db.flush()

    content = digest.build_digest(db, entrance)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert body.index(seed.UNASSIGNED_SPACE_NAME) < body.index("abn"), (
        "an unfilled ABN is listed above a form that 500s"
    )


def test_every_blocking_column_has_a_consequence_sentence():
    """Structural, and it guards a silent failure: rename a column in
    seed.HARD_BLOCK_COLUMNS and the digest quietly stops saying why the
    venue cannot take a booking, with every behavioural test still green.
    venue_readiness raises at import, so this is really asserting that the
    guard is still there to raise."""
    for column in seed.HARD_BLOCK_COLUMNS:
        assert column in venue_readiness.BLOCKING_CONSEQUENCE, (
            f"{column} blocks the venue and the digest has no sentence for it"
        )
