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

    # get_token, not the old get_staff_by_app_token. These two tests used to
    # assert on that one, which production had stopped calling -- so the
    # refusal that actually runs had nothing behind it, and deleting it left
    # the whole suite green.
    assert staff_auth.get_token(db, raw) is None


def test_a_normal_token_still_resolves(db, hamilton, staff_user):
    """The other half -- a refusal that refused everything would pass the
    test above while locking every phone out."""
    raw = staff_auth.issue_app_token(db, staff_user, hamilton)

    assert staff_auth.get_token(db, raw) is not None


# --- a floor account with no venue -----------------------------------------
#
# create_or_update_staff_user's docstring states this as a rule: "a floor
# account with no venue cannot sign into the floor app at all --
# venue_for_token has nothing to give it and no right to guess."
#
# venue_for_token did not implement it. The branch read
# `if staff.venue_id is not None`, which asks "does this account have a
# venue", not "is this an admin" -- so a FLOOR account with a NULL venue
# fell into the admin branch and was handed the full picker: every building
# in the database, on a casual's phone.
#
# The row is reachable. The staff/token migration plans for a rollback in
# its own docstring, and a floor account created on the old build during
# that window writes no venue_id at all.


def test_a_floor_account_with_no_venue_is_refused_not_offered_every_building(
    db, hamilton, entrance
):
    """THE one. NULL means "every venue" for an admin, because that is what
    an admin is. It cannot also mean "a floor account nobody finished
    setting up"."""
    casual = staff_auth.create_or_update_staff_user(
        db, email="casual.noven@test", name="Casual", password="floorpassword1",
        role="floor", venue=hamilton,
    )
    casual.venue_id = None  # the rollback-window row
    db.flush()

    with pytest.raises(staff_auth.VenueRequired) as exc:
        staff_auth.venue_for_token(db, casual, None)

    assert "no venue recorded" in str(exc.value.args[0]), exc.value.args
    assert not exc.value.choices, (
        f"a floor account with no venue was offered a choice of buildings: {exc.value.choices}"
    )


def test_naming_a_venue_does_not_let_a_venueless_floor_account_in_either(
    db, hamilton, entrance
):
    """The refusal has to hold when the request supplies a slug, which is
    the shape the app actually sends after the picker."""
    casual = staff_auth.create_or_update_staff_user(
        db, email="casual.noven2@test", name="Casual", password="floorpassword1",
        role="floor", venue=hamilton,
    )
    casual.venue_id = None
    db.flush()

    with pytest.raises(staff_auth.VenueRequired):
        staff_auth.venue_for_token(db, casual, entrance.slug)


def test_an_admin_is_still_asked_which_venue(db, hamilton, entrance, staff_user):
    """The other direction, and the reason the branch is on ROLE. An admin
    legitimately carries NULL and must be offered the choice -- a fix that
    refused every NULL would lock the only person who can create accounts
    out of the floor app."""
    with pytest.raises(staff_auth.VenueRequired) as exc:
        staff_auth.venue_for_token(db, staff_user, None)

    assert set(exc.value.choices) == {hamilton.slug, entrance.slug}


def test_a_floor_account_with_its_venue_still_signs_in(db, hamilton, entrance):
    """And the ordinary path, so a refusal that refused everything could not
    pass the tests above on its own."""
    ruby = staff_auth.create_or_update_staff_user(
        db, email="ruby.ok@test", name="Ruby", password="floorpassword1",
        role="floor", venue=entrance,
    )
    db.flush()

    assert staff_auth.venue_for_token(db, ruby, None) is entrance


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


# --- the by-id writes, which the LIST being scoped said nothing about ------
#
# staff_list scopes both its queries, so Hamilton's page never RENDERS an
# Entrance casual or an Entrance device. Four POSTs then took a bare id:
# resend-welcome, deactivate, reactivate and token revoke. A device not on
# the page at all was revocable from that page's URL.
#
# Not a privilege escalation while staff_may_use is `role == "admin"` -- but
# that function exists as the one place that changes on the day it is not,
# and these bypassed the scope rather than passing it. booking_in_scope was
# deliberately made a DEPENDENCY for exactly this reason.


def _csrf_in(html: str) -> str:
    import re

    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _token_for(db, staff, venue):
    raw = staff_auth.issue_app_token(db, staff, venue)
    return db.scalars(
        select(StaffAppToken).where(StaffAppToken.token_hash == staff_auth._hash_token(raw))
    ).one()


def test_one_venues_page_cannot_deactivate_another_venues_casual(
    admin_client, db, hamilton, entrance
):
    ruby = staff_auth.create_or_update_staff_user(
        db, email="ruby.byid@test", name="Ruby", password="floorpassword1",
        role="floor", venue=entrance,
    )
    db.flush()
    token = _csrf_in(admin_client.get("/admin/hamilton/staff", follow_redirects=True).text)

    resp = admin_client.post(
        f"/admin/hamilton/staff/{ruby.id}/deactivate",
        data={"csrf_token": token}, follow_redirects=False,
    )

    assert resp.status_code == 404, resp.status_code
    db.refresh(ruby)
    assert ruby.is_active is True, "another venue's casual was deactivated"


def test_one_venues_page_cannot_revoke_another_venues_device(
    admin_client, db, hamilton, entrance
):
    """The one that is least visible: the token is not rendered on this page
    at all, so there is nothing on screen to have clicked."""
    ruby = staff_auth.create_or_update_staff_user(
        db, email="ruby.dev@test", name="Ruby", password="floorpassword1",
        role="floor", venue=entrance,
    )
    db.flush()
    device = _token_for(db, ruby, entrance)
    page = admin_client.get("/admin/hamilton/staff", follow_redirects=True).text
    assert str(device.id) not in page, "the device IS on the page; this test proves nothing"

    resp = admin_client.post(
        f"/admin/hamilton/staff/tokens/{device.id}/revoke",
        data={"csrf_token": _csrf_in(page)}, follow_redirects=False,
    )

    assert resp.status_code == 404
    db.refresh(device)
    assert device.revoked_at is None, "another venue's device was revoked"


def test_a_venue_can_still_act_on_its_own_people_and_devices(
    admin_client, db, hamilton, entrance
):
    """The other direction, so a check that refused everything could not
    pass the tests above on its own."""
    karly = staff_auth.create_or_update_staff_user(
        db, email="karly.byid@test", name="Karly", password="floorpassword1",
        role="floor", venue=hamilton,
    )
    db.flush()
    device = _token_for(db, karly, hamilton)
    page = admin_client.get("/admin/hamilton/staff", follow_redirects=True).text
    token = _csrf_in(page)

    assert admin_client.post(
        f"/admin/hamilton/staff/{karly.id}/deactivate",
        data={"csrf_token": token}, follow_redirects=False,
    ).status_code == 303
    assert admin_client.post(
        f"/admin/hamilton/staff/tokens/{device.id}/revoke",
        data={"csrf_token": token}, follow_redirects=False,
    ).status_code == 303

    db.refresh(karly)
    db.refresh(device)
    assert karly.is_active is False
    assert device.revoked_at is not None


def test_an_admin_is_reachable_from_every_venues_page(admin_client, db, hamilton, entrance, staff_user):
    """StaffUser.venue_id NULL means EVERY venue -- the one place NULL means
    that in this codebase. An admin appears on both pages because they work
    at both, so a scoping fix that keyed on "has a venue" would have made
    them unreachable from either."""
    other = staff_auth.create_or_update_staff_user(
        db, email="second.admin@test", name="Second Admin", password="adminpassword1",
        role="admin",
    )
    db.flush()
    assert other.venue_id is None

    page = admin_client.get("/admin/entrance/staff", follow_redirects=True).text
    resp = admin_client.post(
        f"/admin/entrance/staff/{other.id}/deactivate",
        data={"csrf_token": _csrf_in(page)}, follow_redirects=False,
    )

    assert resp.status_code == 303, resp.status_code
    db.refresh(other)
    assert other.is_active is False
