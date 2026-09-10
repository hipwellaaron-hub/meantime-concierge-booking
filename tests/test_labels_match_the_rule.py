"""Screens that describe a rule narrower than the code enforces.

Aaron, 2026-09-10, on finding one: "It described something narrower than
the rule, which is the same class as an empty field looking like a lost
one." A label is a promise about behaviour, and a wrong one sends somebody
confidently into a refusal -- or, worse, away from a screen believing
something was saved.

Three found by sweeping every admin and client label against its enforcing
line, and each pinned here.
"""

import datetime as dt
import re

import pytest

from app.models import Contact, StaffUser
from app.services import staff_auth
from app.services.booking import create_booking


def _booking(db, space, name="Labels"):
    contact = Contact(name=name, email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 10, 9),
        start_time=dt.time(12, 0), end_time=dt.time(16, 30), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _csrf(client, booking_id):
    return re.search(
        r'name="csrf_token" value="([^"]+)"', client.get(f"/admin/bookings/{booking_id}").text
    ).group(1)


# --- the agreed MINIMUM SPEND, twin of the guest-minimum one ------------------


def test_the_food_minimum_label_says_differs_not_changed(admin_client, db, loft):
    """It read "required if changed" three headings below the guest one that
    said "required if reduced" -- and the rule for both is `!= standard`."""
    booking = _booking(db, loft, "Labels Food Min")

    page = admin_client.get(f"/admin/bookings/{booking.id}").text

    assert "required if changed" not in page, "the food minimum still states a narrower rule"
    assert page.count("differs from the standard") >= 2, "both minimum forms should say the real rule"


def test_saving_an_unchanged_non_standard_food_minimum_still_needs_a_reason(admin_client, db, loft):
    """The case the old label sent staff into: nothing about the AMOUNT is
    changing, so "required if changed" says no reason is needed -- and the
    rule refuses it, because the amount differs from the space standard."""
    booking = _booking(db, loft, "Labels Food Unchanged")
    csrf = _csrf(admin_client, booking.id)
    other = booking.space.min_food_spend + 500

    admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-food-minimum",
        data={"csrf_token": csrf, "agreed_min_food_spend": str(other), "reason": "aaron_discretion"},
        follow_redirects=False,
    )
    db.refresh(booking)
    assert booking.agreed_min_food_spend == other

    # Same amount, reason cleared. Nothing "changed" -- and it is refused.
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-food-minimum",
        data={"csrf_token": csrf, "agreed_min_food_spend": str(other), "reason": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 422
    assert "differs from the space standard" in resp.text


# --- "Add an account" is also an overwrite ------------------------------------


def test_creating_an_account_on_an_existing_email_is_reported_as_an_update(admin_client, db):
    """create_or_update_staff_user resets the password, sets the role and
    forces is_active back to True. The page called every one of those
    "Floor account created"."""
    staff_auth.create_or_update_staff_user(
        db, email="karly@meantime.com.au", name="Karly", password="floorpassword1", role="floor"
    )
    staff_auth.deactivate_staff_user(db, email="karly@meantime.com.au")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin_client.get("/admin/staff").text).group(1)

    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf, "name": "Karly", "email": "karly@meantime.com.au",
            "password": "adifferentpassword", "role": "admin",
        },
        follow_redirects=True,
    )

    assert "outcome=updated" not in resp.text  # it is a redirect target, not body text
    assert "Existing account updated" in resp.text, "an overwrite was reported as a creation"
    # And it really did overwrite, which is why the wording matters.
    again = staff_auth.get_by_email(db, "karly@meantime.com.au")
    assert again.is_active is True, "a deactivated account was silently reactivated"
    assert again.role == "admin", "the role was silently changed"
    assert staff_auth.authenticate(db, "karly@meantime.com.au", "adifferentpassword") is not None


def test_a_genuinely_new_account_still_reads_as_created(admin_client, db):
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin_client.get("/admin/staff").text).group(1)

    resp = admin_client.post(
        "/admin/staff/create",
        data={
            "csrf_token": csrf, "name": "Sally", "email": "sally@meantime.com.au",
            "password": "floorpassword1", "role": "floor",
        },
        follow_redirects=True,
    )

    assert "Existing account updated" not in resp.text
    assert "created" in resp.text


def test_the_form_says_an_existing_email_is_an_overwrite(admin_client, db):
    page = admin_client.get("/admin/staff").text

    assert "Add or update an account" in page
    assert "password is reset" in page
    assert "reactivated" in page


# --- the wizard never calls an unsaved step safe ------------------------------


def test_save_for_later_rethrows_a_real_failure_instead_of_reporting_safety():
    """A client-facing promise, checked in the source because it is browser
    JavaScript with no server round trip to assert on.

    The handler swallowed EVERY error, not just the browser-validation one,
    so a dropped connection or a 500 on the current step still ran on to
    "Saved -- come back any time / Everything you've entered so far is
    safe." over content that never reached the server. That is Preston's
    failure with a reassuring message on top.

    The chain already ends in a .catch that re-enables the button and says
    "Couldn't save right now", so the fix is to let a real error reach it.
    """
    import pathlib

    source = pathlib.Path("app/templates/wizard/wizard.html").read_text(encoding="utf-8")
    start = source.index("SAVE & COME BACK LATER")
    block = source[start:start + 3000]

    handler = block[block.index("var saveCurrent"):block.index("saveCurrent.then")]
    assert "handledByBrowser" in handler, "the browser-validation case must still be tolerated"
    assert "throw err" in handler, "a real save failure is still being swallowed"

    # And the reassurance is still downstream of it, which is the point.
    assert block.index("throw err") < block.index("Everything you\\'ve entered so far is safe")
