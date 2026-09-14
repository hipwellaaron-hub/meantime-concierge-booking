"""A bump-in confirmed after the Event Order went out stops saying
"requested".

Aaron, on Adam Williams: the Event Order printed "DJ bump-in 5:45pm
(requested -- not yet confirmed)" with the event twelve days away, while
the Music field on the same page said the DJ was arriving at 5:45pm to set
up. He had confirmed it on 2 September.

The timeline bullets are composed once by build_event_timeline and frozen
into the document. _refresh_draft_beo_timeline re-composes them when a
bump-in is confirmed -- but only on a DRAFT, so an Event Order that has
already gone out never learns. One document, two answers.

Fixed the way the deposit was: computed AT RENDER, so it is true of every
Event Order that already exists without regenerating any of them, and
there is no route context for the six render sites to forget.

ONLY THE QUALIFIER MOVES. The time, the vendor name and the contact number
stay exactly as composed. Rebuilding the timeline here would discard
anything a person typed into it -- which is the mistake the regeneration
guard exists to prevent.
"""
import datetime as dt

from app.models.booking_vendor import BookingVendor
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking
from app.templating import beo_timeline_bullets

REQUESTED = "(requested — not yet confirmed)"


def _booking(db, loft, contact, name="Bump In"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=12), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _vendor(db, booking, name="DJ Luccette", confirmed=False):
    v = BookingVendor(
        booking_id=booking.id, vendor_type="dj", name=name,
        contact_number=None, bump_in_time=dt.time(17, 45),
        bump_in_confirmed=confirmed, source="wizard",
    )
    db.add(v)
    db.flush()
    return v


def _sent_beo(db, booking):
    from app.services.document_generation import build_vendor_snapshot

    content = beo_proposals.fresh_beo_content(db, booking)
    snapshot = build_vendor_snapshot(booking.vendors)
    from app.services.document_generation import build_event_timeline

    content["vendors"] = snapshot
    content["event_timeline"] = build_event_timeline(booking, snapshot)
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()
    return doc


# --- the fix ----------------------------------------------------------------


def test_a_sent_event_order_learns_the_bump_in_was_confirmed(db, hamilton, loft, contact):
    """THE one. Adam Williams' shape exactly."""
    booking = _booking(db, loft, contact, name="ZZBUMPIN Confirmed")
    vendor = _vendor(db, booking, confirmed=False)
    doc = _sent_beo(db, booking)

    frozen = doc.content["event_timeline"]["bullets"]
    assert any(REQUESTED in line for line in frozen), "fixture did not produce a requested bullet"

    vendor.bump_in_confirmed = True
    db.flush()

    rendered = beo_timeline_bullets(doc)

    assert not any(REQUESTED in line for line in rendered), "it still says requested"
    assert any("(confirmed)" in line and "DJ Luccette" in line for line in rendered)


def test_a_genuinely_unconfirmed_bump_in_still_says_requested(db, hamilton, loft, contact):
    """The qualifier exists because a client routinely nominates a time
    their DJ never agreed to. It must keep saying so."""
    booking = _booking(db, loft, contact, name="ZZBUMPIN Unconfirmed")
    _vendor(db, booking, confirmed=False)
    doc = _sent_beo(db, booking)

    rendered = beo_timeline_bullets(doc)

    assert any(REQUESTED in line for line in rendered)


def test_one_vendor_confirming_does_not_confirm_another(db, hamilton, loft, contact):
    """Matched on the vendor's own name, so a second vendor still waiting
    is left alone."""
    booking = _booking(db, loft, contact, name="ZZBUMPIN Two")
    confirmed = _vendor(db, booking, name="DJ Luccette", confirmed=False)
    _vendor(db, booking, name="Sweet Cheeks Cakes", confirmed=False)
    doc = _sent_beo(db, booking)

    confirmed.bump_in_confirmed = True
    db.flush()

    rendered = beo_timeline_bullets(doc)
    dj = [l for l in rendered if "DJ Luccette" in l][0]
    cake = [l for l in rendered if "Sweet Cheeks Cakes" in l][0]

    assert "(confirmed)" in dj
    assert REQUESTED in cake, "confirming one vendor confirmed another"


def test_only_the_qualifier_changes(db, hamilton, loft, contact):
    """Not a rebuild: the time, the name and everything a person typed into
    the timeline survive."""
    booking = _booking(db, loft, contact, name="ZZBUMPIN Surgical")
    vendor = _vendor(db, booking, confirmed=False)
    doc = _sent_beo(db, booking)
    doc.content["event_timeline"]["bullets"] = list(doc.content["event_timeline"]["bullets"]) + [
        "8:30pm — Speeches (typed by a person)"
    ]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(doc, "content")
    db.flush()

    vendor.bump_in_confirmed = True
    db.flush()
    rendered = beo_timeline_bullets(doc)

    dj = [l for l in rendered if "DJ Luccette" in l][0]
    assert "5:45pm" in dj, "the bump-in time was lost"
    assert "DJ Luccette" in dj
    assert any("typed by a person" in l for l in rendered), "a hand-typed timeline line was dropped"


def test_a_document_with_no_vendors_is_unchanged(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZBUMPIN NoVendors")
    doc = _sent_beo(db, booking)

    assert beo_timeline_bullets(doc) == list(doc.content["event_timeline"]["bullets"])
