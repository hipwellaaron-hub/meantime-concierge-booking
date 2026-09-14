"""The floor's warnings reach the copy that gets printed, and cover the
fields that actually move.

Two gaps in the floor's staleness note, found in the same sweep.

  * THE PDF CARRIED NONE OF THEM. The block in document.html is gated on
    `is_floor_app`, and the PDF route passes `floor_pdf` instead -- so the
    copy that gets printed and left on a bar had no "not yet approved", no
    RSA line and no "the booking has changed since this version". Exactly
    the copy nobody re-checks against a screen.

  * THE NOTE COMPARED NO TIMES. It listed under-18s, date, rooms, adults
    and event name, and the same app's booking detail screen shows every
    time LIVE -- so a start or end time moved after approval was invisible
    on the run sheet AND absent from the note, and the two screens a
    bartender flicks between disagreed with nothing saying so. The bar
    credit had the same shape: a promise the floor honours on the night,
    frozen into the document like the rest of the order.

EVERY PROBE MOVES THE BOOKING AFTER THE VERSION WAS BUILT. An unmoved
booking produces no drift at all, so a note that compared nothing would
pass.
"""
import datetime as dt
from decimal import Decimal

from app.api.staff_app import _floor_drift
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import change_status, create_booking


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=20), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    change_status(db, b, BookingStatus.confirmed, actor="test")
    return b


def _approved_beo(db, booking):
    from app.services.document_generation import build_event_timeline, build_vendor_snapshot

    content = beo_proposals.fresh_beo_content(db, booking)
    content["event_timeline"] = build_event_timeline(booking, build_vendor_snapshot(booking.vendors))
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()
    documents_service.sign(db, doc, signer_name="Test Client", signer_ip="127.0.0.1")
    db.flush()
    return doc


# --- what the note compares ------------------------------------------------------


def test_a_moved_start_time_is_on_the_note(db, hamilton, loft, contact):
    """THE one. The run sheet's own timeline is the snapshot; the booking
    detail screen beside it is live."""
    booking = _booking(db, loft, contact, "ZZFLOOR Start")
    doc = _approved_beo(db, booking)

    booking.start_time = dt.time(19, 30)
    db.flush()

    drift = _floor_drift(doc, booking)

    assert any("start time is now 7:30pm" in d for d in drift), drift


def test_a_moved_end_time_is_on_the_note(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZFLOOR End")
    doc = _approved_beo(db, booking)

    booking.end_time = dt.time(23, 45)
    db.flush()

    assert any("end time is now 11:45pm" in d for d in _floor_drift(doc, booking)), _floor_drift(doc, booking)


def test_a_raised_bar_credit_is_on_the_note(db, hamilton, loft, contact):
    """A promise the floor honours on the night, frozen into the document
    like the rest of the order."""
    booking = _booking(db, loft, contact, "ZZFLOOR BarCredit")
    doc = _approved_beo(db, booking)

    booking.bar_credit = Decimal("500.00")
    db.flush()

    assert any("bar credit is now $500.00" in d for d in _floor_drift(doc, booking)), _floor_drift(doc, booking)


def test_an_unmoved_booking_produces_no_drift(db, hamilton, loft, contact):
    """The positive control. A note that always says something is a note
    nobody reads."""
    booking = _booking(db, loft, contact, "ZZFLOOR Unmoved")
    doc = _approved_beo(db, booking)

    assert _floor_drift(doc, booking) == []


def test_the_existing_comparisons_still_fire(db, hamilton, loft, contact):
    """Adding to the list must not have displaced what was there."""
    booking = _booking(db, loft, contact, "ZZFLOOR Existing")
    doc = _approved_beo(db, booking)

    booking.adult_count = 90
    booking.child_count = 4
    booking.event_name = "Renamed Event"
    db.flush()
    drift = _floor_drift(doc, booking)

    assert any("under-18s are now 4" in d for d in drift)
    assert any("adults are now 90" in d for d in drift)
    assert any("Renamed Event" in d for d in drift)


# --- and they reach the printed copy ------------------------------------------------


def test_the_printed_copy_carries_the_drift_note(db, hamilton, loft, contact):
    """THE one. This is the copy that gets left on a bar.

    The renderer is stubbed rather than run: what is under test is which
    CONTEXT the route hands the template, and driving xhtml2pdf to get at
    it would test the PDF library.
    """
    from fastapi.testclient import TestClient

    from app.api import staff_app
    from app.database import get_db
    from app.main import app
    from app.services import staff_auth
    from tests.test_staff_app import FLOOR_EMAIL, FLOOR_PASSWORD

    staff_auth.create_or_update_staff_user(
        db, email=FLOOR_EMAIL, name="Casual Floor", password=FLOOR_PASSWORD,
        role="floor", venue=hamilton,
    )
    booking = _booking(db, loft, contact, "ZZFLOOR PDF")
    _approved_beo(db, booking)
    booking.adult_count = 90
    db.flush()

    rendered = {}

    def _capture(html):
        rendered["html"] = html
        return b"%PDF-1.4 stub"

    original = staff_app.render_html_to_pdf
    staff_app.render_html_to_pdf = _capture
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        token = client.post(
            "/api/staff/login", json={"email": FLOOR_EMAIL, "password": FLOOR_PASSWORD}
        ).json()["token"]
        response = client.get(
            f"/api/staff/bookings/{booking.id}/beo.pdf",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        staff_app.render_html_to_pdf = original
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert "The booking has changed since this version" in rendered["html"], (
        "the printed copy carries none of the screen's warnings"
    )
    assert "adults are now 90" in rendered["html"]
    assert "Approved by the client" in rendered["html"]
