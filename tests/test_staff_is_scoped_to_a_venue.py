"""Staff and their floor devices are scoped to a venue.

This file was a GATE: it skipped on one venue and failed the moment a second
existed with staff still unscoped, and its message said to replace it with
real scoping tests once step 8 was done. Step 8 is done, so these are those
tests -- the gate having fired correctly and told the next person what to do
is the whole reason it was written as a test rather than a note.

THE RULE, from Aaron 2026-09-12:
  * A FLOOR account belongs to ONE venue. Never asked, cannot choose.
  * An ADMIN carries NULL, meaning EVERY venue, and picks at sign-in.

StaffUser.venue_id is the one column in this codebase where NULL means
something ("every venue") rather than "nobody has said". StaffAppToken's
does not: a phone is in one building, and a token with no venue is refused.
"""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Space, StaffAppToken, Venue
from app.models.staff_user import StaffUser
from app.services import staff_auth
from tests.conftest import STAFF_TEST_PASSWORD


@pytest.fixture()
def entrance(db, hamilton):
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
    db.flush()
    return venue


def _floor(db, email, venue):
    user = staff_auth.create_or_update_staff_user(
        db, email=email, name=f"Floor {email}", password="floorpassword1", role="floor"
    )
    user.venue_id = venue.id
    db.flush()
    return user


# --- who belongs where -----------------------------------------------------


def test_a_floor_account_belongs_to_one_venue(db, hamilton, entrance):
    ham = _floor(db, "ham.floor@test", hamilton)

    assert ham.venue_id == hamilton.id
    assert ham.venue_id != entrance.id


def test_an_admin_carries_no_venue_which_means_every_venue(db, hamilton, staff_user):
    """The exception to "NULL invents nothing". Aaron is the only admin and
    works both buildings."""
    assert staff_user.role == "admin"
    assert staff_user.venue_id is None


# --- issuing a device token ------------------------------------------------


def test_a_floor_token_takes_the_accounts_own_venue(db, hamilton, entrance):
    ham = _floor(db, "ham2.floor@test", hamilton)

    venue = staff_auth.venue_for_token(db, ham, None)

    assert venue.id == hamilton.id, "a floor account was not given its own venue"


def test_a_floor_account_cannot_choose_another_venue(db, hamilton, entrance):
    """THE guard. A casual who works one building must not be able to put the
    other building's run sheets on their phone by editing a request."""
    ham = _floor(db, "ham3.floor@test", hamilton)

    with pytest.raises(staff_auth.VenueRequired, match="not at that venue"):
        staff_auth.venue_for_token(db, ham, "entrance")


def test_an_admin_must_say_which_venue(db, hamilton, entrance, staff_user):
    """No default. Picking one for them is how a phone shows the wrong
    venue's run sheets all night."""
    # The REASON matters, not just the exception type. Removing the
    # "no slug given" branch entirely still raises VenueRequired -- it falls
    # through to "no such venue" when it looks up slug=None -- so a test
    # matching only the type passes over a deleted guard.
    with pytest.raises(staff_auth.VenueRequired, match="which venue") as exc:
        staff_auth.venue_for_token(db, staff_user, None)

    assert set(exc.value.choices) == {"hamilton", "entrance"}


def test_an_admin_who_names_a_venue_gets_it(db, hamilton, entrance, staff_user):
    assert staff_auth.venue_for_token(db, staff_user, "entrance").id == entrance.id
    assert staff_auth.venue_for_token(db, staff_user, "hamilton").id == hamilton.id


def test_a_token_records_the_venue_it_was_issued_for(db, hamilton, entrance, staff_user):
    raw = staff_auth.issue_app_token(db, staff_user, entrance)

    token = db.scalars(
        select(StaffAppToken).where(StaffAppToken.token_hash == staff_auth._hash_token(raw))
    ).one()
    assert token.venue_id == entrance.id


def test_a_token_cannot_be_issued_without_a_venue(db, hamilton, staff_user):
    with pytest.raises(staff_auth.VenueRequired):
        staff_auth.issue_app_token(db, staff_user, None)


# --- what a device with no venue gets --------------------------------------


def test_a_venueless_token_is_refused_rather_than_guessed(db, hamilton, staff_user):
    """A token predating the column, or minted by a rolled-back build, cannot
    be told which venue's run sheets to show. The app handles a 401 by
    clearing the token and showing sign-in, which is the honest outcome."""
    raw = staff_auth.issue_app_token(db, staff_user, hamilton)
    token = db.scalars(
        select(StaffAppToken).where(StaffAppToken.token_hash == staff_auth._hash_token(raw))
    ).one()
    token.venue_id = None
    db.flush()

    assert staff_auth.get_staff_by_app_token(db, raw) is None


def test_a_normal_token_still_resolves(db, hamilton, staff_user):
    """The other half -- a refusal that refused everything would pass the
    test above while locking every phone out."""
    raw = staff_auth.issue_app_token(db, staff_user, hamilton)

    assert staff_auth.get_staff_by_app_token(db, raw) is not None


# --- the admin staff page --------------------------------------------------


def test_the_staff_page_lists_this_venues_people_and_not_the_others(
    admin_client, db, hamilton, entrance
):
    _floor(db, "zzhamonly@test", hamilton)
    _floor(db, "zzentonly@test", entrance)
    db.flush()

    ham = admin_client.get(f"/admin/{hamilton.slug}/staff", follow_redirects=True).text
    ent = admin_client.get(f"/admin/{entrance.slug}/staff", follow_redirects=True).text

    assert "zzhamonly@test" in ham
    assert "zzhamonly@test" not in ent, "a Hamilton floor account appeared at The Entrance"
    assert "zzentonly@test" in ent
    assert "zzentonly@test" not in ham


def test_an_admin_appears_on_every_venues_staff_page(admin_client, db, hamilton, entrance, staff_user):
    """Because they do work at every venue. This is the NULL-means-every
    behaviour, asserted rather than assumed."""
    for slug in (hamilton.slug, entrance.slug):
        page = admin_client.get(f"/admin/{slug}/staff", follow_redirects=True).text
        assert staff_user.email in page, f"the admin is missing from {slug}'s staff page"


def test_a_device_token_is_listed_only_under_its_own_venue(
    admin_client, db, hamilton, entrance, staff_user
):
    """A phone is in one building. Listing it on both pages would make
    "revoke that device" ambiguous."""
    staff_auth.issue_app_token(db, staff_user, entrance)
    db.flush()

    ham = admin_client.get(f"/admin/{hamilton.slug}/staff", follow_redirects=True).text
    ent = admin_client.get(f"/admin/{entrance.slug}/staff", follow_redirects=True).text

    # The revoke control names the token id, so count the controls rather
    # than guess at wording.
    assert ham.count("/tokens/") < ent.count("/tokens/"), (
        "a device signed in at The Entrance also showed on Hamilton's page"
    )


def test_the_staff_page_no_longer_claims_to_span_every_venue(db):
    """The sentence added while this was unscoped is now FALSE, and a
    disproved label is fixed in the same commit that disproves it."""
    import pathlib

    source = pathlib.Path("app/templates/admin/staff_users.html").read_text(encoding="utf-8")

    assert "shared across every venue" not in source, (
        "the page still says staff accounts span every venue, which stopped "
        "being true when admin_staff gained its venue predicate"
    )


# --- creating an account through the admin form ----------------------------
#
# The tests above build accounts by setting venue_id directly, which is fine
# for asserting what the RULES are but cannot exercise the path that applies
# them. Three mutations survived because of exactly that: the admin form
# could have stopped passing its venue entirely and nothing would have gone
# red. These go through the form.


def _csrf(admin_client, path):
    import re

    return re.search(
        r'name="csrf_token" value="([^"]+)"', admin_client.get(path, follow_redirects=True).text
    ).group(1)


def _create_via_form(admin_client, venue, *, email, role):
    path = f"/admin/{venue.slug}/staff"
    return admin_client.post(
        f"{path}/create",
        data={
            "csrf_token": _csrf(admin_client, path),
            "name": f"Made {email}", "email": email,
            "password": "floorpassword1", "role": role,
        },
        follow_redirects=True,
    )


def test_a_floor_account_created_on_a_venues_page_belongs_to_that_venue(
    admin_client, db, hamilton, entrance
):
    """THE path that applies the rule. A floor account created with no venue
    cannot sign into the floor app at all -- venue_for_token has nothing to
    give it and no right to guess -- so the form must pass the venue whose
    page it was submitted from."""
    _create_via_form(admin_client, entrance, email="formfloor@test", role="floor")

    made = db.scalars(select(StaffUser).where(StaffUser.email == "formfloor@test")).one()

    assert made.venue_id == entrance.id, (
        "a floor account created on The Entrance's page did not belong to it"
    )


def test_an_admin_created_on_a_venues_page_is_still_every_venue(
    admin_client, db, hamilton, entrance
):
    """The other direction, and the one that would quietly break Aaron.
    Pinning an admin to the building whose page they happened to be on would
    stop them opening the other one."""
    _create_via_form(admin_client, entrance, email="formadmin@test", role="admin")

    made = db.scalars(select(StaffUser).where(StaffUser.email == "formadmin@test")).one()

    assert made.venue_id is None, (
        "an admin was pinned to a venue; NULL means every venue and an admin works everywhere"
    )


def test_a_floor_account_created_that_way_can_actually_sign_in(admin_client, db, hamilton):
    """End to end: the account the form makes must work on a phone. This is
    what a floor account with no venue would fail."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    _create_via_form(admin_client, hamilton, email="signsin@test", role="floor")

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).post(
            "/api/staff/login", json={"email": "signsin@test", "password": "floorpassword1"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["venue"] == (hamilton.trading_name or hamilton.name)
    finally:
        app.dependency_overrides.clear()
