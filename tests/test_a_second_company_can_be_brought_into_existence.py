"""There is now a path to create and fill in a venue, and it refuses the two things it must.

THERE WAS NO PATH AT ALL. app/seed.py creates Hamilton and only Hamilton;
nothing else in the application has ever created a Venue row; and there is
no Railway exec or CLI from the build machine. So the second company --
Nice Try Events Pty Ltd, trading as Meantime The Entrance, two weeks out --
could only be brought into existence by typing SQL into a database console:
one statement, fourteen columns, no validation, on the row whose contents
print on every invoice and contract that company will ever issue.

The readiness check says what is missing. This is where it gets filled in,
which is what makes that check actionable instead of a dead end.

THE TWO REFUSALS ARE THE POINT, and each guards a string a client holds:

  * the SLUG is not editable at all. It is in the admin URL of every page
    about the venue, in the Stripe endpoint path its account posts to, and
    in AI_VENUE_SLUG.
  * the REFERENCE PREFIX locks the moment the venue has issued a booking or
    an invoice. Every reference built from it is frozen at issue -- the
    invoice trigger refuses to rewrite one -- so a venue that changed it
    mid-life would carry two prefixes with no record of when.

AND CREATING A VENUE CREATES ITS TRIAGE SPACE, because a venue without one
serves its public enquiry form as a 200 and 500s on the submit, losing the
lead with no booking and no notification. That is plumbing, not a decision
anybody should be asked to make. A BOOKABLE room IS a decision and is not
invented here.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import seed
from app.models import Booking, Invoice, Space, Venue
from app.services import venue_readiness


def _venues_url(hamilton):
    return f"/admin/{hamilton.slug}/venues"


def _csrf(client, hamilton):
    """The session-bound token, read off the page itself. Every write here
    goes through require_csrf, so a probe that omitted it would be asserting
    on the CSRF guard rather than on anything this page does."""
    import re as _re

    page = client.get(_venues_url(hamilton))
    return _re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _post(client, hamilton, path, data):
    payload = {"csrf_token": _csrf(client, hamilton), **data}
    return client.post(path, data=payload, follow_redirects=False)


# --- creation -----------------------------------------------------------


def test_a_venue_can_be_created_at_all(admin_client, db, hamilton):
    r = _post(admin_client, hamilton, _venues_url(hamilton), {"name": "The Entrance", "slug": "entrance"})

    assert r.status_code == 303
    created = db.scalar(select(Venue).where(Venue.slug == "entrance"))
    assert created is not None
    assert created.name == "The Entrance"


def test_creating_a_venue_creates_its_triage_space(admin_client, db, hamilton):
    """THE one that stops a lead being lost. Without this space the new
    venue's enquiry form renders and then 500s on submit."""
    _post(admin_client, hamilton, _venues_url(hamilton), {"name": "The Entrance", "slug": "entrance"})

    created = db.scalar(select(Venue).where(Venue.slug == "entrance"))
    spaces = db.scalars(select(Space).where(Space.venue_id == created.id)).all()

    assert [s.name for s in spaces] == [seed.UNASSIGNED_SPACE_NAME]
    assert spaces[0].is_bookable is False


def test_a_new_venue_is_reported_not_ready(admin_client, db, hamilton):
    """It has a triage space and nothing else, and the readiness check --
    the same one /healthz and the digest read -- must say so. A page that
    created a venue and then called it ready would be worse than no page."""
    _post(admin_client, hamilton, _venues_url(hamilton), {"name": "The Entrance", "slug": "entrance"})

    created = db.scalar(select(Venue).where(Venue.slug == "entrance"))
    readiness = venue_readiness.check(db, created)

    assert readiness.is_ready is False
    assert "reference_prefix" in readiness.gaps
    assert "a bookable space" in readiness.gaps
    assert seed.UNASSIGNED_SPACE_NAME not in readiness.gaps


def test_a_duplicate_slug_is_refused(admin_client, db, hamilton):
    r = _post(admin_client, hamilton, _venues_url(hamilton), {"name": "Another Hamilton", "slug": hamilton.slug})

    assert r.status_code == 422


@pytest.mark.parametrize("slug", ["the entrance", "entrance/", "ent..rance", "ent%20"])
def test_a_slug_that_is_not_url_safe_is_refused(admin_client, db, hamilton, slug):
    """It becomes a URL segment and a Stripe endpoint path."""
    r = _post(admin_client, hamilton, _venues_url(hamilton), {"name": "The Entrance", "slug": slug})

    assert r.status_code == 422
    assert db.scalar(select(Venue).where(Venue.name == "The Entrance")) is None


# --- editing ------------------------------------------------------------


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(name="The Entrance", slug="entrance")
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name=seed.UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    db.flush()
    return venue


def test_the_identity_columns_can_be_filled_in(admin_client, db, hamilton, entrance):
    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {
            "trading_name": "Meantime The Entrance",
            "legal_name": "Nice Try Events Pty Ltd",
            "abn": "28 647 750 892",
            "bank_bsb": "062-000",
            "reference_prefix": "ent",
        })

    assert r.status_code == 303
    db.refresh(entrance)
    assert entrance.trading_name == "Meantime The Entrance"
    assert entrance.legal_name == "Nice Try Events Pty Ltd"
    assert entrance.abn == "28 647 750 892"
    assert entrance.reference_prefix == "ENT", "the prefix is not upper-cased"


def test_a_blank_field_is_stored_as_nothing_not_as_empty(admin_client, db, hamilton, entrance):
    """An HTML form posts "" for a field nobody filled in. Stored as "",
    the row would read unfilled to the readiness check (which treats
    whitespace as missing) while looking filled in the table."""
    entrance.abn = "28 647 750 892"
    db.flush()

    _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"abn": "   "})

    db.refresh(entrance)
    assert entrance.abn is None


def test_the_slug_cannot_be_edited(admin_client, db, hamilton, entrance):
    """Not by omission -- by construction. It is in the admin URL of every
    page about this venue, in the Stripe endpoint its account posts to, and
    in AI_VENUE_SLUG."""
    _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"slug": "somewhere-else", "name": "Renamed"})

    db.refresh(entrance)
    assert entrance.slug == "entrance"
    assert entrance.name == "The Entrance", "the internal name was editable too"


def test_the_prefix_can_be_set_before_anything_uses_it(admin_client, db, hamilton, entrance):
    """The positive control for the lock below: without it, a page that
    refused every prefix change would pass that probe."""
    _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"reference_prefix": "ENT"})

    db.refresh(entrance)
    assert entrance.reference_prefix == "ENT"


def test_another_venues_bookings_do_not_lock_this_one(admin_client, db, hamilton, entrance, booking):
    """The lock is about THIS venue's references. Hamilton has a booking
    here and The Entrance has none, so The Entrance's prefix is still
    settable -- and it is the one being set up.

    Without this probe a lock that counted every booking in the database
    passed, because in every other probe the only venue with bookings was
    the one under test. It would have made the second venue's prefix
    permanently unsettable the moment the first venue took an enquiry --
    which is to say, always."""
    assert booking.venue_id == hamilton.id

    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"reference_prefix": "ENT"})

    assert r.status_code == 303, (
        "Hamilton's bookings locked The Entrance's prefix"
    )
    db.refresh(entrance)
    assert entrance.reference_prefix == "ENT"


def test_the_prefix_locks_once_a_booking_exists(admin_client, db, hamilton, entrance, contact):
    """A booking reference is a string a client quotes back on the phone and
    is never rewritten."""
    from app.services.booking import create_booking

    entrance.reference_prefix = "ENT"
    room = Space(
        venue_id=entrance.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(room)
    db.flush()
    import datetime as dt

    create_booking(
        db, space_id=room.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Entrance Party",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()

    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"reference_prefix": "XXX"})

    assert r.status_code == 422
    db.refresh(entrance)
    assert entrance.reference_prefix == "ENT"


def test_resubmitting_the_same_locked_prefix_is_not_an_error(admin_client, db, hamilton, entrance, contact):
    """The form posts every field on every save, so saving a phone number
    on a venue with bookings must not be refused for "changing" a prefix
    that did not change."""
    import datetime as dt

    from app.services.booking import create_booking

    entrance.reference_prefix = "ENT"
    room = Space(
        venue_id=entrance.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(room)
    db.flush()
    create_booking(
        db, space_id=room.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Entrance Party",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()

    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"reference_prefix": "ENT", "phone": "02 4333 0000"})

    assert r.status_code == 303
    db.refresh(entrance)
    assert entrance.phone == "02 4333 0000"


def test_trading_days_can_be_emptied_deliberately(admin_client, db, hamilton, entrance):
    """[] means "open no days" and NULL means "nobody has said". They are
    different answers and a form that could only ever write NULL would lose
    one of them."""
    entrance.trading_days = [2, 3, 4, 5, 6]
    db.flush()

    _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"trading_days_present": "1"})

    db.refresh(entrance)
    assert entrance.trading_days == []


def test_trading_days_are_stored_as_weekday_numbers(admin_client, db, hamilton, entrance):
    _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"trading_days_present": "1", "day_2": "on", "day_5": "on"})

    db.refresh(entrance)
    assert entrance.trading_days == [2, 5]


def test_an_unknown_venue_is_404(admin_client, hamilton):
    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{uuid.uuid4()}", {"abn": "x"})

    assert r.status_code == 404


# --- the page -----------------------------------------------------------


def test_the_page_lists_every_venue_and_names_its_gaps(admin_client, db, hamilton, entrance):
    """It is about every venue while sitting under one venue's URL, which
    is the exception here -- so each row has to name the company it is
    about rather than leave it to the band."""
    r = admin_client.get(_venues_url(hamilton))

    assert r.status_code == 200
    assert "The Entrance" in r.text
    assert (hamilton.trading_name or hamilton.name) in r.text
    assert "reference_prefix" in r.text, "the new venue's gaps are not named"


def test_the_page_says_what_a_blocking_gap_costs(admin_client, db, hamilton, entrance):
    r = admin_client.get(_venues_url(hamilton))

    assert "cannot take a booking or issue an invoice" in r.text


def test_a_locked_prefix_is_not_an_editable_field_on_the_page(
    admin_client, db, hamilton, entrance, contact
):
    import datetime as dt

    from app.services.booking import create_booking

    entrance.reference_prefix = "ENT"
    room = Space(
        venue_id=entrance.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(room)
    db.flush()
    create_booking(
        db, space_id=room.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Entrance Party",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()

    r = admin_client.get(_venues_url(hamilton))

    assert "Locked:" in r.text


def test_the_page_needs_a_login(db, hamilton):
    """It writes a company's bank details."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        r = TestClient(app).get(_venues_url(hamilton), follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert r.status_code in (302, 303, 401, 403), (
        f"the venue set-up page served {r.status_code} to nobody"
    )


def test_a_write_without_a_csrf_token_is_refused(admin_client, db, hamilton, entrance):
    r = _post(admin_client, hamilton, f"{_venues_url(hamilton)}/{entrance.id}", {"abn": "x", "csrf_token": "not-the-token"})

    assert r.status_code == 403
    db.refresh(entrance)
    assert entrance.abn != "x"


def test_the_digest_link_to_this_page_resolves(raw_admin_client, db, hamilton):
    """The digest names an UNSCOPED /admin/venues, deliberately -- every
    link it builds is unscoped so it works out its own venue and cannot go
    stale when forwarded. That only holds if the unscoped path exists.

    raw_admin_client, not admin_client: admin_client rewrites
    /admin/<section>/... onto the venue segment, so this probe would never
    reach the compat route and would pass because of the rewrite.
    """
    r = raw_admin_client.get("/admin/venues", follow_redirects=False)

    assert r.status_code == 303, f"the digest's link is a {r.status_code}"
    assert r.headers["location"].endswith("/venues"), r.headers["location"]


def test_the_digest_actually_builds_that_link(db, hamilton):
    """And the email names it. Asserted end to end rather than by reading
    the format string: this line was WRONG within hours of being written
    the first time -- it said no admin page edited these, which stopped
    being true the same afternoon."""
    from app.services import digest

    hamilton.abn = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "https://x/admin/venues" in body
