"""Three more frozen values on the Event Order, read at render instead.

beo_timeline_bullets repaired the VENDOR bump-in qualifier and left two
things beside it:

  * SETUP ACCESS. build_event_timeline composes two qualifiers one line
    apart, worded differently -- a vendor says "(requested — not yet
    confirmed)" and setup access says "(requested, pending confirmation)".
    Only the first was repaired, so a sent Event Order went on saying setup
    access was unconfirmed after staff had confirmed it. The identical
    fault, in the bullet directly above the one that was fixed.

  * THE VENDOR NAME MATCH was a bare substring. A vendor called "DJ"
    matched every line mentioning a DJ and confirmed all of them. Anchored
    on the separator build_vendor_snapshot actually composes now.

And the AV USB DEADLINE, which is a frozen arithmetic result: build_av
stored `event_date - 14 days` as a display string, so postponing the event
left the client with an instruction whose deadline came from the old date
-- wrong in either direction and silent both ways.

EVERY PROBE STARTS FROM A SENT DOCUMENT. A draft is re-composed by
_refresh_draft_beo_timeline, so a draft would print the right thing
whatever the render-time repair does.
"""
import datetime as dt

from app.models.booking_vendor import BookingVendor
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking
from app.templating import beo_av_deadline, beo_timeline_bullets

REQUESTED_VENDOR = "(requested — not yet confirmed)"
REQUESTED_SETUP = "(requested, pending confirmation)"


def _booking(db, loft, contact, name, *, setup=dt.time(14, 0), days=21):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=days), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    b.setup_access_time = setup
    b.setup_access_confirmed = False
    db.flush()
    return b


def _vendor(db, booking, name, *, confirmed=False):
    v = BookingVendor(
        booking_id=booking.id, vendor_type="dj", name=name, contact_number=None,
        bump_in_time=dt.time(17, 45), bump_in_confirmed=confirmed, source="wizard",
    )
    db.add(v)
    db.flush()
    return v


def _sent_beo(db, booking):
    from app.services.document_generation import build_event_timeline, build_vendor_snapshot

    content = beo_proposals.fresh_beo_content(db, booking)
    snapshot = build_vendor_snapshot(booking.vendors)
    content["vendors"] = snapshot
    content["event_timeline"] = build_event_timeline(booking, snapshot)
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()
    return doc


# --- setup access ---------------------------------------------------------------


def test_a_sent_event_order_learns_setup_access_was_confirmed(db, hamilton, loft, contact):
    """THE one, and the bullet directly above the one already fixed."""
    booking = _booking(db, loft, contact, "ZZSETUP Confirmed")
    doc = _sent_beo(db, booking)
    assert any(REQUESTED_SETUP in b for b in doc.content["event_timeline"]["bullets"]), (
        "fixture did not produce a pending setup-access bullet"
    )

    booking.setup_access_confirmed = True
    db.flush()

    rendered = beo_timeline_bullets(doc)

    assert not any(REQUESTED_SETUP in b for b in rendered), "it still says pending confirmation"
    assert any("Setup access from" in b and "(confirmed)" in b for b in rendered)


def test_an_unconfirmed_setup_access_still_says_pending(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZSETUP Pending")
    doc = _sent_beo(db, booking)

    assert any(REQUESTED_SETUP in b for b in beo_timeline_bullets(doc))


def test_confirming_setup_access_does_not_confirm_a_vendor(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZSETUP NotVendor")
    _vendor(db, booking, "DJ Luccette")
    doc = _sent_beo(db, booking)

    booking.setup_access_confirmed = True
    db.flush()
    rendered = beo_timeline_bullets(doc)

    assert any(REQUESTED_VENDOR in b for b in rendered), "a vendor was confirmed by the setup access"
    assert any("Setup access from" in b and "(confirmed)" in b for b in rendered)


# --- the vendor name match ---------------------------------------------------------


def test_a_short_vendor_name_does_not_confirm_another_vendors_line(db, hamilton, loft, contact):
    """A bare substring match confirmed every line containing the name. The
    vendor here is called "DJ", which appears in the OTHER vendor's line as
    the type label."""
    booking = _booking(db, loft, contact, "ZZMATCH Short")
    short = _vendor(db, booking, "DJ")
    _vendor(db, booking, "Sweet Cheeks Cakes")
    doc = _sent_beo(db, booking)

    short.bump_in_confirmed = True
    db.flush()
    rendered = beo_timeline_bullets(doc)

    cake = [b for b in rendered if "Sweet Cheeks Cakes" in b][0]
    assert REQUESTED_VENDOR in cake, f"a vendor named 'DJ' confirmed another vendor's line: {cake}"


def test_the_named_vendors_own_line_still_moves(db, hamilton, loft, contact):
    """The positive control: anchoring must not stop the real match."""
    booking = _booking(db, loft, contact, "ZZMATCH Real")
    vendor = _vendor(db, booking, "DJ Luccette")
    doc = _sent_beo(db, booking)

    vendor.bump_in_confirmed = True
    db.flush()

    dj = [b for b in beo_timeline_bullets(doc) if "DJ Luccette" in b][0]
    assert "(confirmed)" in dj


def test_a_vendor_with_a_contact_number_still_matches(db, hamilton, loft, contact):
    """The composed shape is "... — {name}, {contact}", so the anchor has
    to allow what follows the name."""
    booking = _booking(db, loft, contact, "ZZMATCH Contact")
    vendor = _vendor(db, booking, "DJ Luccette")
    vendor.contact_number = "0400 000 000"
    db.flush()
    doc = _sent_beo(db, booking)

    vendor.bump_in_confirmed = True
    db.flush()

    dj = [b for b in beo_timeline_bullets(doc) if "DJ Luccette" in b][0]
    assert "(confirmed)" in dj
    assert "0400 000 000" in dj, "the contact number was lost"


# --- the USB deadline ---------------------------------------------------------------


def _with_av(db, booking):
    from app.services.document_generation import build_av_block

    content = beo_proposals.fresh_beo_content(db, booking)
    booking.space.has_screen = True
    db.flush()
    content["av"] = build_av_block(booking, {"video_slideshow": True, "microphones_for_speeches": False, "notes": None})
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()
    return doc


def test_the_usb_deadline_follows_a_postponed_event(db, hamilton, loft, contact):
    """THE one. The deadline is an instruction with a date in it, derived
    from the event date -- and it was derived once, at generation."""
    from app.services import policy
    from app.services.document_generation import format_day_date

    booking = _booking(db, loft, contact, "ZZUSB Postponed")
    doc = _with_av(db, booking)
    frozen = doc.content["av"]["usb_deadline_display"]

    booking.event_date = booking.event_date + dt.timedelta(days=60)
    db.flush()

    rendered = beo_av_deadline(doc)

    assert rendered != frozen, "the deadline did not follow the event"
    expected = booking.event_date - dt.timedelta(days=policy.AV_USB_DEADLINE_DAYS_BEFORE_EVENT)
    assert rendered == format_day_date(expected)


def test_an_unmoved_event_keeps_the_same_deadline(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZUSB Unmoved")
    doc = _with_av(db, booking)

    assert beo_av_deadline(doc) == doc.content["av"]["usb_deadline_display"]


def test_a_document_with_no_av_block_has_no_deadline(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZUSB NoAV")
    doc = _sent_beo(db, booking)

    assert beo_av_deadline(doc) is None


def test_the_rendered_event_order_prints_the_recomputed_deadline(db, hamilton, loft, contact):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app
    from app.services import policy
    from app.services.document_generation import format_day_date

    booking = _booking(db, loft, contact, "ZZUSB Rendered")
    doc = _with_av(db, booking)
    frozen = doc.content["av"]["usb_deadline_display"]
    booking.event_date = booking.event_date + dt.timedelta(days=60)
    db.flush()

    app.dependency_overrides[get_db] = lambda: db
    try:
        page = TestClient(app).get(f"/d/{doc.access_token}").text
    finally:
        app.dependency_overrides.clear()

    expected = format_day_date(booking.event_date - dt.timedelta(days=policy.AV_USB_DEADLINE_DAYS_BEFORE_EVENT))
    assert f"USB due by {expected}" in page
    assert frozen not in page, "the frozen deadline is still on the client's page"
