"""Fixing a vendor's name does not un-confirm their bump-in.

The Event Order edit form's vendor sync matched existing rows on the
(vendor_type, name) PAIR. So a rename was a DELETE plus a CREATE: correct
"DJ Micheal" to "DJ Michael" and the confirmed row was removed and a new
one made with bump_in_confirmed=False. Changing the TYPE did the same.

The code's own comment has always said an edit "keeps its source and its
confirmation unless the bump-in time itself changed". Keying on the name
broke that promise silently, and the screen then showed "requested" as if
nobody had ever checked the time against setup access and the day's other
bookings -- which is exactly what a confirmation is for.

The row carries its own id on the form now. A new row posts an empty one,
and an id that does not belong to this booking is ignored rather than
trusted: it arrived over the wire.
"""
import datetime as dt
import re

import pytest
from sqlalchemy import select

from app.models import BookingVendor
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.document_generation import generate_beo_content


@pytest.fixture()
def beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


@pytest.fixture()
def confirmed_dj(db, booking):
    row = BookingVendor(
        booking_id=booking.id,
        vendor_type="dj",
        name="DJ Micheal",
        contact_number="0400111222",
        bump_in_time=dt.time(16, 0),
        bump_in_confirmed=True,
        source="staff",
    )
    db.add(row)
    db.flush()
    return row


def _save(admin_client, db, booking, document, **overrides):
    """Post the edit form the way the page renders it."""
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    # THE IDS COME OFF THE RENDERED PAGE, not out of the database. Taking
    # them from booking.vendors made every probe here pass with the hidden
    # field deleted from the template -- the save would have been keying on
    # ids the real form never sends. Mutation-checked: that version
    # survived removing the field.
    rendered_ids = re.findall(r'name="vendor_ids" value="([^"]*)"', page.text)
    vendors = list(booking.vendors)
    assert len([i for i in rendered_ids if i]) == len(vendors), (
        f"the edit page rendered {rendered_ids} for {len(vendors)} vendor row(s) -- "
        "the form is not carrying each row's id"
    )
    data = {
        "csrf_token": token,
        "content_expect": expect,
        "catering_order_and_service_style": "",
        "bar_structure": "",
        "room_layout_notes": "",
        "music": "",
        "entertainment": "",
        "music_entertainment": "",
        "special_notes": "",
        "dietaries": "",
        "accessibility": "",
        "decorations": "",
        "status_text": "",
        "onsite_contact": "",
        "internal_notes": "",
        "guest_arrival_time": "",
        "pack_down_notes": "",
        "vendor_ids": [i for i in rendered_ids if i],
        "vendor_types": [v.vendor_type for v in vendors],
        "vendor_names": [v.name for v in vendors],
        "vendor_contacts": [v.contact_number or "" for v in vendors],
        "vendor_bump_ins": [v.bump_in_time.strftime("%H:%M") if v.bump_in_time else "" for v in vendors],
    }
    data.update(overrides)
    return admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=data,
        follow_redirects=False,
    )


def test_a_rename_keeps_the_confirmation(admin_client, db, booking, beo, confirmed_dj):
    """THE one."""
    original_id = confirmed_dj.id

    response = _save(admin_client, db, booking, beo, vendor_names=["DJ Michael"])
    assert response.status_code == 303, response.text[:400]

    rows = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    assert len(rows) == 1, "the rename created a second row instead of editing one"
    assert rows[0].id == original_id, "the row was deleted and recreated"
    assert rows[0].name == "DJ Michael", "the rename did not take"
    assert rows[0].bump_in_confirmed is True, (
        "fixing a typo in the name un-confirmed a bump-in somebody had checked"
    )


def test_changing_the_type_keeps_the_confirmation(admin_client, db, booking, beo, confirmed_dj):
    original_id = confirmed_dj.id

    response = _save(admin_client, db, booking, beo, vendor_types=["band"])
    assert response.status_code == 303

    rows = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].vendor_type == "band"
    assert rows[0].bump_in_confirmed is True


def test_moving_the_bump_in_time_still_un_confirms(admin_client, db, booking, beo, confirmed_dj):
    """The rule that must NOT change, and the control for the two above: a
    confirmation is about the TIME. Move it and it has to be checked
    again."""
    response = _save(admin_client, db, booking, beo, vendor_bump_ins=["17:30"])
    assert response.status_code == 303

    row = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).one()
    assert row.bump_in_time == dt.time(17, 30)
    assert row.bump_in_confirmed is False, (
        "the bump-in time moved and the old confirmation was carried over"
    )


def test_blanking_the_name_still_removes_the_row(admin_client, db, booking, beo, confirmed_dj):
    """How the form deletes a vendor. Keying on the id must not break it."""
    response = _save(admin_client, db, booking, beo, vendor_names=[""])
    assert response.status_code == 303

    rows = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    assert rows == []


def test_a_new_row_with_no_id_is_still_added(admin_client, db, booking, beo, confirmed_dj):
    """The add-a-vendor template posts an empty id."""
    response = _save(
        admin_client, db, booking, beo,
        vendor_ids=[str(confirmed_dj.id), ""],
        vendor_types=["dj", "decorator"],
        vendor_names=["DJ Micheal", "Bloom & Co"],
        vendor_contacts=["0400111222", ""],
        vendor_bump_ins=["16:00", "14:00"],
    )
    assert response.status_code == 303

    rows = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    by_name = {r.name: r for r in rows}
    assert set(by_name) == {"DJ Micheal", "Bloom & Co"}
    assert by_name["DJ Micheal"].bump_in_confirmed is True, "the untouched row lost its confirmation"
    assert by_name["Bloom & Co"].bump_in_confirmed is False, "a brand-new bump-in is not confirmed"


def test_an_id_from_another_booking_is_not_adopted(admin_client, db, booking, beo, confirmed_dj, loft, contact):
    """The id arrives over the wire. A row belonging to somebody else's
    booking must not be edited, or moved onto this one."""
    from app.services.booking import create_booking

    other = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 8, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Someone Else",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="test",
    )
    theirs = BookingVendor(
        booking_id=other.id, vendor_type="dj", name="Their DJ",
        bump_in_time=dt.time(15, 0), bump_in_confirmed=True, source="staff",
    )
    db.add(theirs)
    db.flush()

    response = _save(
        admin_client, db, booking, beo,
        vendor_ids=[str(theirs.id)],
        vendor_types=["dj"],
        vendor_names=["Hijacked"],
        vendor_contacts=[""],
        vendor_bump_ins=["16:00"],
    )
    assert response.status_code == 303

    db.refresh(theirs)
    assert theirs.name == "Their DJ", "another booking's vendor row was edited"
    assert theirs.booking_id == other.id
    assert theirs.bump_in_confirmed is True
    # And this booking got a new row of its own rather than adopting theirs.
    mine = db.scalars(select(BookingVendor).where(BookingVendor.booking_id == booking.id)).all()
    assert [r.name for r in mine] == ["Hijacked"]
    assert mine[0].id != theirs.id
