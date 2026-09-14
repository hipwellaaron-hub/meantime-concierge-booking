"""Saving onto a document that was sent while you had it open keeps your words.

TWO REFUSALS ON ONE ROUTE, opposite treatment of the same typing.

The fingerprint conflict hands the staff member's own text back, and says
why in terms: "throwing a JSON error at a staff member who has just typed
a long note would protect one person's writing by destroying another's, in
a feature whose whole purpose is that neither happens."

Ten lines earlier, the already-sent check raised a bare 409. Sally has the
Event Order editor open with the night's run-sheet notes in it; Karly
sends the BEO from another tab; Sally presses Save and gets an error page.
Everything she typed is gone -- and unlike the conflict path there is not
even a draft to save again, so there is nothing to go back to.

It comes back as the form holding what she wrote, with a banner that says
what happened and points at Revise. A sent document is never rewritten
underneath the client, so Revise is genuinely the way forward; what
changes is that her words survive the trip.
"""
import re

import pytest
from sqlalchemy import select

from app.models.document import Document, DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services.document_generation import generate_agreement_content, generate_beo_content

TYPED = "Cake table by the window at 7. Sparklers OFF -- venue rule, tell the DJ."


@pytest.fixture()
def beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


def _post_edit(admin_client, booking, document, **overrides):
    """Post the edit form.

    The token comes from the BOOKING page when the editor will not render,
    which is the already-sent case -- the GET refuses a non-draft too, and
    that is correct and unchanged. What this exercises is the POST, which
    is the one a staff member reaches with a page already open and a
    paragraph already typed.
    """
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    if page.status_code != 200:
        page = admin_client.get(f"/admin/bookings/{booking.id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    found = re.search(r'name="content_expect" value="([^"]*)"', page.text)
    expect = found.group(1) if found else ""
    data = {
        "csrf_token": token, "content_expect": expect,
        "catering_order_and_service_style": "", "bar_structure": "",
        "room_layout_notes": "", "music": "", "entertainment": "",
        "music_entertainment": "", "special_notes": "", "dietaries": "",
        "accessibility": "", "decorations": "", "status_text": "",
        "onsite_contact": "", "internal_notes": "",
        "guest_arrival_time": "", "pack_down_notes": "",
    }
    data.update(overrides)
    return admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=data, follow_redirects=False,
    )


def test_the_typing_comes_back_when_the_document_was_sent(admin_client, db, booking, beo):
    """THE one. The page is rendered, then the document is sent, then the
    save lands -- which is the real sequence, not a contrivance."""
    # Render the form first, the way a staff member has it open.
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{beo.id}/edit")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    documents_service.mark_sent(db, beo, actor="staff:karly")
    db.commit()

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{beo.id}/edit",
        data={
            "csrf_token": token, "content_expect": expect,
            "catering_order_and_service_style": "", "bar_structure": "",
            "room_layout_notes": "", "music": "", "entertainment": "",
            "music_entertainment": "", "special_notes": TYPED, "dietaries": "",
            "accessibility": "", "decorations": "", "status_text": "",
            "onsite_contact": "", "internal_notes": "",
            "guest_arrival_time": "", "pack_down_notes": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409, "the save must still be refused"
    assert TYPED in response.text, (
        "the staff member's typing was thrown away, which is what the "
        "neighbouring refusal on this same route exists not to do"
    )
    assert "sent while you had it open" in response.text
    assert "Revise" in response.text, "the screen does not say what to do next"


def test_the_document_is_not_written_to(admin_client, db, booking, beo):
    """Handing the words back must not be mistaken for accepting them."""
    documents_service.mark_sent(db, beo, actor="staff:karly")
    db.commit()
    before = dict(beo.content)

    _post_edit(admin_client, booking, beo, special_notes=TYPED)

    db.refresh(beo)
    assert beo.content == before, "a sent document was edited"
    assert beo.status is DocumentStatus.sent


def test_a_cleared_field_stays_cleared_on_the_way_back(admin_client, db, booking, beo):
    """Every submitted value comes back, including the empty ones.
    Overlaying only the truthy ones puts back text somebody had just
    deleted -- the conflict path learnt that and this is the same rule."""
    documents_service.update_content_fields(
        db, beo, {"decorations": "Balloon arch"}, actor="staff:test",
        authored_fields=["decorations"],
    )
    documents_service.mark_sent(db, beo, actor="staff:karly")
    db.commit()

    response = _post_edit(admin_client, booking, beo, decorations="", special_notes=TYPED)

    assert response.status_code == 409
    assert "Balloon arch" not in response.text, (
        "the form came back holding a value the staff member had just cleared"
    )


def test_an_agreement_sent_mid_edit_keeps_its_clauses(admin_client, db, booking):
    """The agreement editor posts clause pairs rather than prose fields, so
    it needs its own probe: a fix that only handled the Event Order would
    lose a hand-negotiated contract edit instead."""
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    documents_service.mark_sent(db, agreement, actor="staff:karly")
    db.commit()

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit",
        data={
            "csrf_token": token, "content_expect": expect,
            "headings": ["Minimum Spend"],
            "bodies": ["Negotiated down to $8,000 for this date only."],
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Negotiated down to $8,000" in response.text


def test_a_normal_save_on_a_draft_still_works(admin_client, db, booking, beo):
    """The positive control, and it earns its place: a route that answered
    409 for everything would pass every probe above."""
    response = _post_edit(admin_client, booking, beo, special_notes=TYPED)

    assert response.status_code == 303
    db.refresh(beo)
    assert beo.content["special_notes"] == TYPED


def test_a_document_on_another_booking_is_still_a_404(admin_client, db, booking, loft, contact):
    """The sent-document branch reads the document by id before the draft
    check, so it has to make the same scoping decision -- otherwise it
    becomes a way to render one booking's document under another's URL."""
    import datetime as dt

    from app.services.booking import create_booking

    other = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 9, 4),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Another Booking",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="test",
    )
    theirs = documents_service.create_new_version(
        db, other, DocumentType.beo, generate_beo_content(other), actor="staff:test"
    )
    documents_service.mark_sent(db, theirs, actor="staff:test")
    db.commit()

    page = admin_client.get(f"/admin/bookings/{booking.id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{theirs.id}/edit",
        data={"csrf_token": token, "special_notes": TYPED},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert TYPED not in response.text
