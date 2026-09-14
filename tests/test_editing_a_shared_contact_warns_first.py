"""Editing a client's details says which other bookings it changes.

Aaron, 2026-09-14, on HAM-20261128-OCXUC: "If I 'fix' that name I could be
rewriting other bookings without knowing... Warn me, list what else
changes, let me decide."

WHY IT IS SHARED. A Contact is one per EMAIL ADDRESS, not one per booking.
So the Change form under a booking's contact line does not edit "this
booking's contact" -- it edits a row that every booking on that address
reads LIVE.

WHAT ACTUALLY MOVES. The Customer Details block on every invoice of every
one of those bookings, including already-sent and already-paid ones,
because an invoice has no bill-to columns and re-renders from the contact
on each view and download. A SIGNED AGREEMENT does not move: it never
prints the contact at all and its signature line uses the stored
signer_name.

WHY NOT SCOPE THE EDIT INSTEAD. Aaron ruled against it, and the reason is
in the reconciliation: one shared contact produces one CONTACT_HYGIENE
finding per booking, so a single correct edit is MEANT to clear them all.
Per-booking scoping would turn one fix into N.

NAMED, NOT COUNTED. "This contact is shared" tells someone to go and find
out what that means. A list tells them what they are about to change.
"""
import datetime as dt

from app.services.booking import create_booking
from app.services.contact_matching import (
    find_or_create_contact,
    other_bookings_on_contact,
)


def _booking(db, loft, contact, name, days=20):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=days), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


# --- the helper -------------------------------------------------------------


def test_it_finds_the_other_bookings_on_the_same_contact(db, hamilton, loft, contact):
    first = _booking(db, loft, contact, "ZZSHARED First", days=20)
    second = _booking(db, loft, contact, "ZZSHARED Second", days=40)
    third = _booking(db, loft, contact, "ZZSHARED Third", days=60)

    others = other_bookings_on_contact(db, first)

    assert {b.id for b in others} == {second.id, third.id}
    assert first.id not in {b.id for b in others}, "it named the booking being edited"


def test_it_is_empty_for_a_contact_with_one_booking(db, hamilton, loft):
    """No warning where there is nothing to warn about -- a banner that
    always shows is a banner nobody reads."""
    solo, _ = find_or_create_contact(db, name="Solo Client", email="solo@example.com", phone=None)
    db.flush()
    booking = _booking(db, loft, solo, "ZZSOLO Only")

    assert other_bookings_on_contact(db, booking) == []


def test_a_booking_with_no_contact_is_not_an_error(db, hamilton, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=20), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name="ZZNOCONTACT", event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()

    assert other_bookings_on_contact(db, booking) == []


def test_a_different_contact_is_not_swept_in(db, hamilton, loft, contact):
    """Scoped by contact_id, not by name or by anything fuzzy."""
    mine = _booking(db, loft, contact, "ZZSCOPE Mine")
    someone_else, _ = find_or_create_contact(db, name="Other Person", email="other@example.com", phone=None)
    db.flush()
    _booking(db, loft, someone_else, "ZZSCOPE Theirs")

    assert other_bookings_on_contact(db, mine) == []


# --- what the page actually shows -------------------------------------------


def test_the_page_names_the_other_bookings(admin_client, db, hamilton, loft, contact):
    """The deliverable. A count would send someone looking; the references
    and dates are what let them decide."""
    first = _booking(db, loft, contact, "ZZWARN First", days=20)
    second = _booking(db, loft, contact, "ZZWARN Second", days=40)

    page = admin_client.get(f"/admin/hamilton/bookings/{first.id}", follow_redirects=True)

    assert page.status_code == 200
    # Whitespace-normalised: the sentence wraps across template lines, and a
    # literal match would fail on the indentation rather than on the content.
    flat = " ".join(page.text.split())
    assert "This contact is shared with 1 other booking." in flat
    assert second.reference_code in page.text, "the other booking is not named"
    assert "ZZWARN Second" in page.text
    assert "already sent or paid" in page.text
    assert "Signed agreements are not affected" in page.text


def test_no_warning_when_the_contact_is_not_shared(admin_client, db, hamilton, loft):
    solo, _ = find_or_create_contact(db, name="Quiet Client", email="quiet@example.com", phone=None)
    db.flush()
    booking = _booking(db, loft, solo, "ZZNOWARN Only")

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)

    assert page.status_code == 200
    assert "This contact is shared" not in page.text


def test_the_warning_says_the_audit_lands_on_this_booking_only(
    admin_client, db, hamilton, loft, contact
):
    """The sibling bookings' documents change with nothing in their own
    trail to say why, which is the part a person would never guess."""
    first = _booking(db, loft, contact, "ZZAUDIT First", days=20)
    _booking(db, loft, contact, "ZZAUDIT Second", days=40)

    page = admin_client.get(f"/admin/hamilton/bookings/{first.id}", follow_redirects=True)

    assert "Only this booking's audit trail will record the change." in page.text
