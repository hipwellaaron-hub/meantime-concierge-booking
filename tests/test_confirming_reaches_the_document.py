"""Confirming a time has to change what the Event Order says.

Setup access and a vendor's bump-in both print as "requested, pending
confirmation" until staff confirm them, and both are composed into the
stored Event Order at generation time. So confirming the booking fact is
only half the job -- the document has to be re-composed or it goes on
saying pending.

The bump-in handler always did that. The setup-access handler never did.
Nothing made the asymmetry visible, and the result was Aaron confirming
setup access to two clients in writing while both Event Orders went on
printing "requested, pending confirmation" (HAM-20260911-AKPSO and
HAM-20260912-2R11Q, 2026-09-08).

A sent or signed document is never mutated -- staff regenerate, per the
existing document rules -- so the refresh only ever touches a draft, and
these tests pin that too.
"""

import datetime as dt
import re

import pytest

from app.models import BookingVendor, Contact
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import build_vendor_snapshot, generate_beo_content

PENDING = "requested, pending confirmation"


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _booking_with_setup_access(db, space, name="Setup Access"):
    contact = Contact(name="Setup Client", email=f"setup.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    # What the wizard's Basics step writes: a time, and a request that is
    # explicitly not a confirmation (Aaron's 2026-08-28 ruling).
    booking.setup_access_time = dt.time(14, 0)
    booking.setup_access_confirmed = False
    db.flush()
    return booking


def _beo(db, booking, *, with_vendors=False):
    # The wizard path passes the vendor snapshot into the generator; the
    # staff path does not, and its BEO carries an empty list until
    # something populates it. Both shapes are real, so both are used here.
    vendors = build_vendor_snapshot(booking.vendors) if with_vendors else None
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking, vendors=vendors), actor="staff:test"
    )


def _timeline(document):
    return str(document.content.get("event_timeline"))


def test_a_requested_setup_time_prints_as_pending(db, loft):
    """The premise, so the fix below is measured against something real."""
    booking = _booking_with_setup_access(db, loft)
    document = _beo(db, booking)

    assert PENDING in _timeline(document)
    assert "Setup access from 2:00pm" in _timeline(document)


def test_confirming_setup_access_reaches_the_draft_event_order(admin_client, db, loft):
    """The defect. The flag flipped and the document never heard about it."""
    booking = _booking_with_setup_access(db, loft, "Setup Confirm")
    document = _beo(db, booking)
    assert PENDING in _timeline(document)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": _csrf(admin_client, booking.id)},
    )
    assert resp.status_code in (200, 303), resp.status_code

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert PENDING not in _timeline(current), "the Event Order still says pending"
    assert "Setup access from 2:00pm (confirmed)" in _timeline(current)


def test_it_leaves_a_sent_event_order_alone(admin_client, db, loft):
    """A document the client already holds is never rewritten underneath
    them -- staff regenerate. Pinned so the fix cannot grow into mutating
    sent documents."""
    booking = _booking_with_setup_access(db, loft, "Setup Sent")
    document = _beo(db, booking)
    documents_service.mark_sent(db, document, actor="staff:test")
    before = _timeline(document)

    admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": _csrf(admin_client, booking.id)},
    )

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert _timeline(current) == before
    assert PENDING in _timeline(current)


def test_confirming_setup_access_does_not_disturb_the_vendors(admin_client, db, loft):
    """It confirms setup access. The vendor snapshot is a different fact
    and must come through untouched."""
    booking = _booking_with_setup_access(db, loft, "Setup Vendors")
    db.add(
        BookingVendor(
            booking_id=booking.id, vendor_type="dj", name="DJ Matt Shepard",
            contact_number="0400 000 000", bump_in_time=dt.time(17, 30),
            bump_in_confirmed=False, source="staff",
        )
    )
    db.flush()
    document = _beo(db, booking, with_vendors=True)
    vendors_before = document.content["vendors"]
    assert vendors_before[0]["bump_in_confirmed"] is False

    # Move the vendor row underneath the document, so a rebuilt snapshot
    # would differ from the stored one. Without this the two are identical
    # and the test cannot tell a rebuild from a carry-through.
    vendor_row = booking.vendors[0]
    vendor_row.bump_in_confirmed = True
    db.flush()

    admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": _csrf(admin_client, booking.id)},
    )

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["vendors"] == vendors_before, "confirming setup access rebuilt the vendors"
    assert current.content["vendors"][0]["bump_in_confirmed"] is False


def test_confirming_a_bump_in_still_refreshes_its_own_snapshot(admin_client, db, loft):
    """The behaviour that already worked, pinned through the shared helper
    so folding the two together did not quietly change it."""
    booking = _booking_with_setup_access(db, loft, "Bump Still")
    vendor = BookingVendor(
        booking_id=booking.id, vendor_type="dj", name="DJ Matt Shepard",
        contact_number="0400 000 000", bump_in_time=dt.time(17, 30),
        bump_in_confirmed=False, source="staff",
    )
    db.add(vendor)
    db.flush()
    document = _beo(db, booking, with_vendors=True)
    assert document.content["vendors"][0]["bump_in_confirmed"] is False
    assert "not yet confirmed" in document.content["vendors"][0]["bump_in_display"]

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/vendors/{vendor.id}/confirm-bump-in",
        data={"csrf_token": _csrf(admin_client, booking.id)},
    )
    assert resp.status_code in (200, 303), resp.status_code

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["vendors"][0]["bump_in_confirmed"] is True
    assert "(confirmed)" in current.content["vendors"][0]["bump_in_display"]


def test_a_booking_with_no_event_order_is_fine(admin_client, db, loft):
    booking = _booking_with_setup_access(db, loft, "Setup No Doc")

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": _csrf(admin_client, booking.id)},
    )

    assert resp.status_code in (200, 303), resp.status_code
    db.refresh(booking)
    assert booking.setup_access_confirmed is True
