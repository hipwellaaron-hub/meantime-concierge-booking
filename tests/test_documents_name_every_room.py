"""A two-room event's documents must name both rooms.

A linked second space (app.services.booking.add_linked_space) is the SAME
event in another room, modelled as a second Booking row. Every document
header printed only the parent's own room.

Live on HAM-20260912-2R11Q: a 121-guest Saturday across The Mezzanine and
The Loft whose Event Order header read "The Mezzanine" while the body of
the same document referred to both rooms repeatedly. The floor board
already printed "The Mezzanine + The Loft" -- so the document the team
carries on the night contradicted the board they work from, and said the
upstairs room was somebody else's.

The Event Order band reads the rooms LIVE, which is deliberate: it is the
only change that fixes a document already sent and viewed without
regenerating it, and a regenerate would rebuild from the booking and
discard hand-entered content. The agreement's copy is frozen at
generation, also deliberate -- a signed contract reflects what was agreed
-- so that one has to be right when it is written.

Guest counts are NOT summed across the link: a linked child carries its
own pax (114+7 on the parent, 100 on the child here), and adding them
would print a number nobody booked.
"""

import datetime as dt

import pytest

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import add_linked_space, change_status, create_booking
from app.services.document_generation import generate_agreement_content, generate_beo_content

BOTH = "The Mezzanine + The Loft"


@pytest.fixture()
def two_room_booking(db, hamilton, mezzanine, loft):
    """The shape of Caitlin's Saturday: The Mezzanine as the parent, The
    Loft linked to it."""
    contact = Contact(name="Two Room Client", email="tworoom@example.com")
    db.add(contact)
    db.flush()
    parent = create_booking(
        db, space_id=mezzanine.id, contact_id=contact.id, event_date=dt.date(2027, 5, 15),
        start_time=dt.time(17, 0), end_time=dt.time(23, 30), event_name="Two Room Party",
        event_type="birthday", adult_count=114, child_count=7, notes=None, actor="test",
    )
    add_linked_space(db, parent, space_id=loft.id, actor="test")
    db.flush()
    db.expire(parent, ["linked_bookings"])
    return parent


def test_the_booking_knows_every_room_it_occupies(two_room_booking):
    assert two_room_booking.all_space_names == BOTH


def test_a_single_room_booking_reads_exactly_as_before(db, loft, hamilton):
    contact = Contact(name="One Room", email="oneroom@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 16),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="One Room Party",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    assert booking.all_space_names == "The Loft"


def test_the_child_names_both_rooms_too(db, two_room_booking):
    """Asked from either row, it is the same event and the same answer."""
    child = two_room_booking.linked_bookings[0]
    assert child.all_space_names == BOTH


@pytest.mark.parametrize("gone", [BookingStatus.cancelled, BookingStatus.dead])
def test_a_cancelled_second_room_drops_off(db, two_room_booking, gone):
    child = two_room_booking.linked_bookings[0]
    change_status(db, child, gone, actor="test", reason="test")
    db.flush()
    db.expire(two_room_booking, ["linked_bookings"])

    assert two_room_booking.all_space_names == "The Mezzanine"


def test_a_completed_event_still_names_both_rooms(db, two_room_booking):
    """The exclusion list is not TERMINAL_STATUSES: a finished two-room
    night's Event Order must still say which rooms it was."""
    child = two_room_booking.linked_bookings[0]
    change_status(db, child, BookingStatus.completed, actor="test")
    db.flush()
    db.expire(two_room_booking, ["linked_bookings"])

    assert two_room_booking.all_space_names == BOTH


# --- what actually prints ------------------------------------------------------


def _render(admin_client, db, booking, doc_type, content):
    document = documents_service.create_new_version(db, booking, doc_type, content, actor="staff:test")
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/preview")
    assert page.status_code == 200, page.status_code
    return document, page.text


def test_the_event_order_header_names_both_rooms(admin_client, db, two_room_booking):
    _, html = _render(
        admin_client, db, two_room_booking, DocumentType.beo, generate_beo_content(two_room_booking)
    )

    assert BOTH in html, "the Event Order header still names one room"


def test_the_agreement_header_names_both_rooms(admin_client, db, two_room_booking):
    _, html = _render(
        admin_client, db, two_room_booking, DocumentType.agreement,
        generate_agreement_content(two_room_booking),
    )

    assert BOTH in html


def test_an_already_generated_event_order_starts_naming_both_rooms(admin_client, db, two_room_booking):
    """The one that matters before Saturday. A document generated when the
    header said one room must print both as soon as this ships, WITHOUT
    being regenerated -- regenerating would rebuild it from the booking and
    throw away everything hand-entered on it."""
    content = generate_beo_content(two_room_booking)
    # Exactly what the stored content looked like before this change.
    content["_reference"] = {**content["_reference"], "space_name": "The Mezzanine"}
    document, html = _render(admin_client, db, two_room_booking, DocumentType.beo, content)

    assert document.content["_reference"]["space_name"] == "The Mezzanine", "stored copy untouched"
    assert BOTH in html, "an already-sent Event Order is still stuck on one room"


def test_the_guest_count_is_not_summed_across_the_link(admin_client, db, two_room_booking):
    """The child holds its own pax. 114 + 7 is the event; 121 + the child's
    own count is a number nobody booked."""
    _, html = _render(
        admin_client, db, two_room_booking, DocumentType.beo, generate_beo_content(two_room_booking)
    )

    assert "Attendees: 121" in html, "the parent's own 114 adults + 7 children"
    assert "221" not in html, "the child's pax were added in"
