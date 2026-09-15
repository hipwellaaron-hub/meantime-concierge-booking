"""The Event Order edit form's compare-and-set now covers the whole form.

content_fingerprint has always covered the prose and, since 2026-09-08, the
food order. The same form also writes:

  * the guest arrival time, the key moments and the pack-down notes --
    onto the BOOKING, not the document;
  * the vendor rows, reconciled against booking_vendors;
  * the AV block, inside the content.

None of that moved anything the fingerprint looked at, so a save decided
against values that had since changed was accepted and reverted them
without a word. The blast radius is wider than "the document": a vendor
row the stale form did not re-post is DELETED outright, and each reverted
timeline fact writes a field_changed audit row naming the staff member
whose save undid it.

WHY IT WAS PARKED, AND WHY IT IS NOT ANY MORE. Aaron, 2026-09-07:
"widening starts refusing saves that today succeed, and the vendor
snapshot is rewritten by a different staff action entirely (the bump-in
confirmation), so fingerprinting it naively would reject every open edit
form for no reason."

That objection is exactly right and it is answered by what the guard
hashes: the vendor row's id, type, name, contact number and bump-in TIME,
and never bump_in_confirmed. Confirming a bump-in is a different button;
it changes only the column this guard ignores, so an open form survives
it. That is the probe at the bottom of this file, and it is the one that
decides whether the whole thing is usable.
"""
import datetime as dt
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import BookingVendor
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.document_generation import generate_beo_content

TYPED = "Cake table by the window at 7. Sparklers OFF."


@pytest.fixture()
def beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


@pytest.fixture()
def dj(db, booking):
    row = BookingVendor(
        booking_id=booking.id, vendor_type="dj", name="DJ Micheal",
        contact_number="0400111222", bump_in_time=dt.time(16, 0),
        bump_in_confirmed=False, source="staff",
    )
    db.add(row)
    db.flush()
    return row


def _form(admin_client, booking, document):
    """The form as the page renders it, ready to post back unchanged."""
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert page.status_code == 200
    ids = [i for i in re.findall(r'name="vendor_ids" value="([^"]*)"', page.text) if i]
    vendors = [v for v in booking.vendors if str(v.id) in ids]
    return {
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
        "content_expect": re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1),
        "form_expect": re.search(r'name="form_expect" value="([^"]*)"', page.text).group(1),
        "catering_order_and_service_style": "", "bar_structure": "",
        "room_layout_notes": "", "music": "", "entertainment": "",
        "music_entertainment": "", "special_notes": "", "dietaries": "",
        "accessibility": "", "decorations": "", "status_text": "",
        "onsite_contact": "", "internal_notes": "",
        "guest_arrival_time": (
            booking.guest_arrival_time.strftime("%H:%M") if booking.guest_arrival_time else ""
        ),
        "pack_down_notes": booking.pack_down_notes or "",
        "vendor_ids": [str(v.id) for v in vendors],
        "vendor_types": [v.vendor_type for v in vendors],
        "vendor_names": [v.name for v in vendors],
        "vendor_contacts": [v.contact_number or "" for v in vendors],
        "vendor_bump_ins": [
            v.bump_in_time.strftime("%H:%M") if v.bump_in_time else "" for v in vendors
        ],
    }


def _post(admin_client, booking, document, form):
    return admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form, follow_redirects=False,
    )


BANNER = "Somebody changed this while you had it open"


def _refusal_names(response):
    """The fields the REFUSAL BANNER lists, not every word on the page.

    Asserting `"Vendors" in response.text` passed with the banner deleted,
    because the form itself has a Vendors heading -- the guard's own
    mutation probe caught that. This reads only inside the banner.
    """
    assert BANNER in response.text, "no refusal banner was rendered at all"
    start = response.text.index(BANNER)
    block = response.text[start:start + 1400]
    return set(re.findall(r"<li><strong>([^<]+)</strong></li>", block))


# --- the losses this closes --------------------------------------------


def test_a_colleagues_new_vendor_is_no_longer_deleted(admin_client, db, booking, beo, dj):
    """THE one. A row the stale form never knew about was reconciled away
    -- not reverted on the document, DELETED from booking_vendors."""
    form = _form(admin_client, booking, beo)

    florist = BookingVendor(
        booking_id=booking.id, vendor_type="decorator", name="Bloom & Co",
        bump_in_time=dt.time(14, 0), bump_in_confirmed=False, source="staff",
    )
    db.add(florist)
    db.flush()

    form["special_notes"] = TYPED
    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 409, "the stale save was accepted"
    assert "Vendors" in _refusal_names(response)
    assert TYPED in response.text, "the refusal threw away the staff member's typing"

    rows = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    assert {r.name for r in rows} == {"DJ Micheal", "Bloom & Co"}, (
        "the colleague's vendor row was deleted by a save that never saw it"
    )


def test_a_colleagues_arrival_time_is_no_longer_reverted(admin_client, db, booking, beo):
    """Written onto the BOOKING, with a field_changed audit row naming the
    person whose save reverted it."""
    from app.models import BookingEvent

    booking.guest_arrival_time = dt.time(18, 0)
    db.flush()
    form = _form(admin_client, booking, beo)

    booking.guest_arrival_time = dt.time(18, 30)
    db.flush()

    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 409
    assert "Guest arrival time" in _refusal_names(response)
    db.refresh(booking)
    assert booking.guest_arrival_time == dt.time(18, 30), "the colleague's change was undone"

    reverts = db.scalars(
        select(BookingEvent).where(
            BookingEvent.booking_id == booking.id,
            BookingEvent.field_name == "guest_arrival_time",
        )
    ).all()
    assert not any(e.new_value == "18:00:00" for e in reverts), (
        "an audit row records the revert as this staff member's own change"
    )


def test_a_colleagues_key_moment_is_no_longer_lost(admin_client, db, booking, beo):
    booking.key_moments = [{"time": "20:00", "label": "Speeches"}]
    db.flush()
    form = _form(admin_client, booking, beo)

    booking.key_moments = [
        {"time": "20:00", "label": "Speeches"},
        {"time": "21:30", "label": "Cake"},
    ]
    db.flush()

    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 409
    assert "Key moments" in _refusal_names(response)
    db.refresh(booking)
    assert len(booking.key_moments) == 2


def test_a_colleagues_av_note_is_no_longer_replaced(admin_client, db, booking, beo):
    form = _form(admin_client, booking, beo)

    content = dict(beo.content)
    av = dict(content.get("av") or {})
    av["notes"] = "Bride's laptop, HDMI, test at 5pm"
    content["av"] = av
    documents_service.update_content_fields(
        db, beo, {"av": av}, actor="staff:other",
    )
    db.flush()

    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 409
    assert "AV" in _refusal_names(response)
    db.refresh(beo)
    assert beo.content["av"]["notes"] == "Bride's laptop, HDMI, test at 5pm"


# --- and it still lets the ordinary day through ------------------------


def test_an_unchanged_form_still_saves(admin_client, db, booking, beo, dj):
    """The positive control. A guard that refused everything would pass
    every probe above and make the page unusable."""
    form = _form(admin_client, booking, beo)
    form["special_notes"] = TYPED

    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 303, response.text[:400]
    db.refresh(beo)
    assert beo.content["special_notes"] == TYPED


def test_confirming_a_bump_in_does_not_refuse_an_open_form(admin_client, db, booking, beo, dj):
    """THE probe that decides whether this guard is usable at all, and the
    exact objection that kept the work parked.

    Confirming a bump-in is a different staff action on a different button,
    and it happens while somebody has the Event Order open. It changes only
    bump_in_confirmed -- which this guard deliberately does not hash -- so
    the open form must still save.
    """
    form = _form(admin_client, booking, beo)

    dj.bump_in_confirmed = True
    db.flush()

    form["special_notes"] = TYPED
    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 303, (
        "confirming a bump-in refused an open edit form -- which is the "
        "objection this design exists to answer"
    )
    db.refresh(dj)
    assert dj.bump_in_confirmed is True, "the save undid the confirmation"


def test_the_staff_members_own_edits_still_save(admin_client, db, booking, beo, dj):
    """Changing the timeline and the vendors YOURSELF is the normal use of
    this form and must not trip a guard about somebody else."""
    form = _form(admin_client, booking, beo)
    form["guest_arrival_time"] = "18:45"
    form["pack_down_notes"] = "Out by midnight"
    form["vendor_names"] = ["DJ Michael"]

    response = _post(admin_client, booking, beo, form)

    assert response.status_code == 303, response.text[:400]
    db.refresh(booking)
    db.refresh(dj)
    assert booking.guest_arrival_time == dt.time(18, 45)
    assert booking.pack_down_notes == "Out by midnight"
    assert dj.name == "DJ Michael"


def test_a_form_with_no_fingerprint_is_not_refused(admin_client, db, booking, beo):
    """A tab opened before this shipped posts no form_expect. The guard
    cannot judge what it was never given, and refusing those would log
    everybody out of their open forms on the deploy."""
    form = _form(admin_client, booking, beo)
    form["form_expect"] = ""
    form["special_notes"] = TYPED

    assert _post(admin_client, booking, beo, form).status_code == 303


def test_the_agreement_form_is_unaffected(admin_client, db, booking):
    """It has no timeline, no vendors and no AV block, and posts no
    form_expect. A guard that applied to it would refuse every agreement
    save."""
    from app.services.document_generation import generate_agreement_content

    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit",
        data={
            "csrf_token": token, "content_expect": expect,
            "headings": ["Minimum Spend"], "bodies": ["Agreed at $8,000."],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text[:400]


# --- the hash itself ----------------------------------------------------


def test_the_fingerprint_ignores_the_confirmation_column(db, booking, beo, dj):
    """Stated on the hash as well as through the route, because this is the
    property the whole design rests on and a route probe could pass for a
    dozen other reasons."""
    before = documents_service.form_side_fingerprint(db, booking, beo.content)

    dj.bump_in_confirmed = True
    db.flush()
    db.refresh(booking)

    assert documents_service.form_side_fingerprint(db, booking, beo.content) == before


def test_the_fingerprint_notices_the_bump_in_time(db, booking, beo, dj):
    """The other half. A confirmation is about the TIME, so the time moving
    is exactly what an open form must not overwrite."""
    before = documents_service.form_side_fingerprint(db, booking, beo.content)

    dj.bump_in_time = dt.time(17, 30)
    db.flush()
    db.refresh(booking)

    assert documents_service.form_side_fingerprint(db, booking, beo.content) != before


def test_the_vendor_rows_are_read_in_a_settled_order(db, booking, beo, dj):
    """Two reads of the same rows must not hash differently because the
    database returned them in another order -- that would refuse saves
    which conflict with nothing, the failure mode that parked this work.

    Asserted on the ORDER the values come back in rather than by reversing
    a list and re-hashing: the second version tests content_fingerprint,
    which was never the thing in doubt.
    """
    # EXPLICIT IDS, in descending order, so "insertion order" and "sorted
    # order" are deterministically different. With random UUIDs the two
    # coincide about half the time, and a sort that did nothing passed --
    # which is exactly what the mutation probe found.
    import uuid as _uuid

    for suffix in ("ffffffffffff", "000000000000"):
        db.add(BookingVendor(
            id=_uuid.UUID(f"00000000-0000-4000-8000-{suffix}"),
            booking_id=booking.id, vendor_type="decorator",
            name=f"Vendor {suffix[:3]}", bump_in_time=dt.time(14, 0), source="staff",
        ))
    db.flush()

    rows = documents_service.form_side_values(db, booking, beo.content)["vendors"]

    assert len(rows) == 3
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows), (
        "the vendor rows are not read in a settled order, so two reads of the "
        "same rows can hash differently and refuse a save that conflicts with "
        "nothing"
    )
    # And it is stable across reads.
    again = documents_service.form_side_values(db, booking, beo.content)["vendors"]
    assert [r["id"] for r in again] == [r["id"] for r in rows]


def test_the_fingerprint_sees_a_row_added_in_this_same_session(db, booking, beo, dj):
    """The bug this cost. form_side_values read booking.vendors, and a
    relationship collection is whatever was loaded the first time it was
    touched -- so in a session that had already rendered the form, a
    colleague's brand-new vendor was invisible and the guard passed over
    a save that deleted it. Queried now."""
    before = documents_service.form_side_fingerprint(db, booking, beo.content)

    db.add(BookingVendor(
        booking_id=booking.id, vendor_type="decorator", name="Bloom & Co",
        bump_in_time=dt.time(14, 0), source="staff",
    ))
    db.flush()

    assert documents_service.form_side_fingerprint(db, booking, beo.content) != before
