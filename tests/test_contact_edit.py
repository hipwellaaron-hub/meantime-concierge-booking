"""Editing a booking's contact details from the admin form.

The bug this pins: the handler read name, email and phone correctly and
passed all three to find_or_create_contact, which -- by design, for lead
intake -- returns an existing contact unchanged when the email matches. So
a staff edit that kept the email (which is exactly what fixing a doubled
name or adding a phone number looks like) silently discarded the new name
and phone, changed no contact_id, and therefore wrote no audit event
either. A successful no-op with no trace.

Email appeared to work only because changing it missed the match and
created a NEW contact row, which is its own problem: an edit should
correct a person's record, not fork it.
"""

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, BookingEvent, Contact
from app.services.booking import create_booking


@pytest.fixture()
def booking_with_contact(db, loft):
    contact = Contact(name="Christine Hipwell Christine Hipwell",
                      email="christine@example.com", phone=None)
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=60),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_name="MS Christmas Party", event_type="corporate",
        adult_count=120, child_count=0, notes=None, actor="staff:test",
    )


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _post_contact(client, booking, *, name, email, phone=""):
    return client.post(
        f"/admin/bookings/{booking.id}/contact",
        data={
            "csrf_token": _csrf(client, booking.id),
            "name": name, "email": email, "phone": phone,
        },
        follow_redirects=False,
    )


def _field_events(db, booking, field):
    return [
        e for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all()
        if e.event_type == "field_changed" and e.field_name == field
    ]


# --- the reported bug ---------------------------------------------------


def test_correcting_a_doubled_name_actually_saves(admin_client, db, booking_with_contact):
    b = booking_with_contact
    contact_id = b.contact_id

    resp = _post_contact(
        admin_client, b, name="Christine Hipwell", email="christine@example.com"
    )
    assert resp.status_code in (302, 303)

    db.expire_all()
    contact = db.get(Contact, contact_id)
    assert contact.name == "Christine Hipwell"
    # Corrected in place, not forked into a second row.
    assert db.get(Booking, b.id).contact_id == contact_id


def test_adding_a_phone_number_actually_saves(admin_client, db, booking_with_contact):
    b = booking_with_contact
    contact_id = b.contact_id

    _post_contact(
        admin_client, b, name="Christine Hipwell Christine Hipwell",
        email="christine@example.com", phone="0412 345 678",
    )

    db.expire_all()
    assert db.get(Contact, contact_id).phone == "0412 345 678"


def test_every_changed_field_writes_an_audit_event(admin_client, db, booking_with_contact):
    """The silent no-op is how this went unnoticed twice. A change with no
    audit trail is the failure mode, not just the missing write."""
    b = booking_with_contact

    _post_contact(
        admin_client, b, name="Christine Hipwell",
        email="christine@example.com", phone="0412 345 678",
    )
    db.expire_all()

    name_events = _field_events(db, b, "contact_name")
    phone_events = _field_events(db, b, "contact_phone")
    assert len(name_events) == 1
    assert name_events[0].old_value == "Christine Hipwell Christine Hipwell"
    assert name_events[0].new_value == "Christine Hipwell"
    assert len(phone_events) == 1
    assert phone_events[0].new_value == "0412 345 678"


def test_an_unchanged_submission_writes_nothing(admin_client, db, booking_with_contact):
    """No spurious events from a form resubmitted unchanged."""
    b = booking_with_contact
    _post_contact(
        admin_client, b, name="Christine Hipwell Christine Hipwell",
        email="christine@example.com",
    )
    db.expire_all()
    assert _field_events(db, b, "contact_name") == []
    assert _field_events(db, b, "contact_phone") == []


def test_correcting_an_email_typo_updates_in_place(admin_client, db, booking_with_contact):
    """Emily's gmail.clm typo: correcting it must fix the contact, not
    leave the old row behind and silently create a second one."""
    b = booking_with_contact
    contact_id = b.contact_id

    _post_contact(
        admin_client, b, name="Christine Hipwell Christine Hipwell",
        email="christine.hipwell@example.com",
    )
    db.expire_all()

    assert db.get(Booking, b.id).contact_id == contact_id
    assert db.get(Contact, contact_id).email == "christine.hipwell@example.com"
    assert len(_field_events(db, b, "contact_email")) == 1


# --- the cases that must keep working -----------------------------------


def test_pointing_a_booking_at_a_different_existing_person(admin_client, db, booking_with_contact, loft):
    """Submitting an email that belongs to somebody else repoints the
    booking to them -- and must NOT rewrite that person's stored name."""
    other = Contact(name="Sally Jones", email="sally@example.com", phone="0400 000 000")
    db.add(other)
    db.flush()
    other_id = other.id

    b = booking_with_contact
    _post_contact(admin_client, b, name="Someone Typed This", email="sally@example.com")
    db.expire_all()

    assert db.get(Booking, b.id).contact_id == other_id
    assert db.get(Contact, other_id).name == "Sally Jones", "must not clobber another record"
    assert len(_field_events(db, b, "contact_id")) == 1


def test_attaching_a_contact_to_a_booking_that_has_none(admin_client, db, loft):
    b = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=61),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_name="No Contact Yet", event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="staff:test",
    )
    _post_contact(admin_client, b, name="New Person", email="new.person@example.com",
                  phone="0499 999 999")
    db.expire_all()

    refreshed = db.get(Booking, b.id)
    assert refreshed.contact_id is not None
    assert refreshed.contact.name == "New Person"
    assert refreshed.contact.phone == "0499 999 999"


def test_a_public_enquiry_still_cannot_rename_an_existing_contact(db, hamilton, loft):
    """The protective behaviour of find_or_create_contact must survive: a
    stranger submitting the enquiry form with a known email must not be
    able to rewrite that person's name or phone."""
    from app.services.contact_matching import find_or_create_contact

    original = Contact(name="Aaron Hipwell", email="aaron@meantime.com.au", phone="0411 111 111")
    db.add(original)
    db.flush()

    contact, _ = find_or_create_contact(db, "Not Aaron", "aaron@meantime.com.au", "0400 000 000")
    assert contact.id == original.id
    assert contact.name == "Aaron Hipwell"
    assert contact.phone == "0411 111 111"
