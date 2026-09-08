"""A keep is an answer to a question that was asked.

The confirmation screen renders one checkbox per loss: those are the
fields the person was shown and asked about. The write filtered the
submitted names against PROTECTED_FIELD_NAMES instead -- all fifteen
protected names, most of which were never on that screen. Any of them
arriving in the POST was written.

`regenerated_note` is built from the losses, so it cannot mention a keep
that was not one. Proved before the fix, on a BEO whose only offered loss
was Room layout notes: a POST also keeping `terms_sections`,
`music_entertainment` and `dietaries` wrote `terms_sections: None` and
`terms_text: None` into a BEO -- keys belonging to the agreement, not this
document -- and froze `music_entertainment` at empty over the [REVIEW]
prompt that would have asked somebody to fill it in. The audit line read
"v2: kept Room layout notes". Three writes, no record.

The same on an agreement: a POST keeping `dietaries` and `internal_notes`
wrote both as None into the contract, under "kept Agreement terms".

This repair existed on 2bbd23e and went away with the revert of that
commit. It is being re-done deliberately rather than rediscovered.
"""

import datetime as dt
import re

from app.models import Contact
from app.models.booking_event import BookingEvent
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import REVIEW, generate_agreement_content, generate_beo_content

LAYOUT = "Rounds of 8."
MUSIC_PROMPT = f"{REVIEW} add music/entertainment detail"


def _booking(db, space, name):
    contact = Contact(name="Keep Test", email=f"keep.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _shown(client, booking_id, doc_type, csrf):
    """The confirmation screen, with the token and the boxes it offered."""
    page = client.post(f"/admin/bookings/{booking_id}/documents/{doc_type}/generate", data={"csrf_token": csrf})
    assert page.status_code == 409, page.status_code
    expect = re.search(r'name="expect" value="([^"]+)"', page.text).group(1)
    return expect, re.findall(r'name="keep" value="([^"]+)"', page.text)


def _note(db, booking_id):
    events = db.query(BookingEvent).filter_by(
        booking_id=booking_id, event_type="document_regenerated"
    ).all()
    return [event.new_value for event in events]


def test_a_keep_for_a_field_nobody_was_asked_about_writes_nothing(admin_client, db, loft):
    booking = _booking(db, loft, "Keep Unoffered")
    content = generate_beo_content(booking)
    content["room_layout_notes"] = LAYOUT
    content["music_entertainment"] = ""
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    expect, offered = _shown(admin_client, booking.id, "beo", csrf)
    assert offered == ["room_layout_notes"], offered

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect,
              "keep": ["room_layout_notes", "terms_sections", "music_entertainment", "dietaries"]},
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)

    # The one field that WAS offered is kept -- the answer still works.
    assert current.content["room_layout_notes"] == LAYOUT
    # The three that were not are simply regenerated, like any field
    # nobody was asked about.
    assert current.content["music_entertainment"] == MUSIC_PROMPT, (
        "an unoffered keep froze the field over the prompt to fill it in"
    )
    assert "terms_sections" not in current.content, "the agreement's terms were written into a BEO"
    assert "terms_text" not in current.content, "the agreement's terms were written into a BEO"


def test_the_audit_line_can_name_everything_the_write_kept(admin_client, db, loft):
    """The property that was broken, stated directly: the note is built
    from the losses, so the write must not keep anything outside them --
    otherwise a value is frozen with nothing anywhere able to say so."""
    booking = _booking(db, loft, "Keep Audit")
    content = generate_beo_content(booking)
    content["room_layout_notes"] = LAYOUT
    content["music_entertainment"] = ""
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    expect, _ = _shown(admin_client, booking.id, "beo", csrf)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": ["room_layout_notes", "music_entertainment"]},
    )
    db.expire_all()
    note = _note(db, booking.id)
    assert note == ["v2: kept Room layout notes"], note
    assert "Music" not in note[0], "the note claims nothing about music, so nothing about music was kept"
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["music_entertainment"] == MUSIC_PROMPT


def test_an_agreement_cannot_be_given_a_beo_field_by_keeping_it(admin_client, db, loft):
    booking = _booking(db, loft, "Keep Agreement")
    content = generate_agreement_content(booking)
    sections = list(content["terms_sections"])
    sections[0] = {**sections[0], "body": "Hand-negotiated clause."}
    content["terms_sections"] = sections
    documents_service.create_new_version(db, booking, DocumentType.agreement, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    expect, offered = _shown(admin_client, booking.id, "agreement", csrf)
    assert offered == ["terms_sections"], offered

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": [*offered, "dietaries", "internal_notes"]},
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.agreement)

    assert "Hand-negotiated clause." in str(current.content["terms_sections"]), "the offered keep still applies"
    assert "dietaries" not in current.content
    assert "internal_notes" not in current.content


def test_the_companion_still_travels_with_an_offered_keep(admin_client, db, loft):
    """The narrowing must not reach the companions. terms_text is rebuilt
    from terms_sections; keeping the sections and letting the text
    regenerate would leave the contract stating two sets of terms."""
    booking = _booking(db, loft, "Keep Companion")
    content = generate_agreement_content(booking)
    sections = list(content["terms_sections"])
    sections[0] = {**sections[0], "body": "Hand-negotiated clause."}
    content["terms_sections"] = sections
    content["terms_text"] = "Hand-negotiated clause."
    documents_service.create_new_version(db, booking, DocumentType.agreement, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    expect, offered = _shown(admin_client, booking.id, "agreement", csrf)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": offered},
    )
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.agreement)
    assert current.content["terms_text"] == "Hand-negotiated clause."
