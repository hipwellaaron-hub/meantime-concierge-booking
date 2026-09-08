"""Superseding a document does not change its STATUS.

That one sentence is the whole trap. A draft that a regenerate replaced is
still `draft`; a sent version that a regenerate replaced is still `sent`. So
every guard in this codebase that asks "is this a draft?" or "is this sent?"
is satisfied by a row nobody will ever read again -- and the row is fetched
by id, so the lock is granted on it too.

beo_proposals._locked_draft has checked is_current since 2026-09-06, with a
comment saying it was proved live that day. The check was never carried to
the other three writers. Each of these was proved by running it:

  - the EDIT FORM wrote a staff member's typing onto a superseded draft and
    returned 303. The live Event Order still said "[REVIEW] add room layout
    notes"; the words were on a row whose link 410s. A save indistinguishable
    from a successful one, and content gone -- the failure this whole area of
    the codebase exists to prevent;
  - SEND flipped a superseded draft to `sent`, so staff had "sent" a link
    that 410s. On an agreement it also takes a room hold off the back of a
    document the client can never open;
  - REVISE copied a superseded version forward, discarding the newer one
    (fixed separately, in f570df0 -- see test_revising_a_sent_document.py).

The tests here are deliberately state-based rather than race-based: no
concurrency is needed to reach any of it. A second browser tab, the Back
button, or a triage list rendered a minute ago is enough.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

TYPED = "Rounds of 8, dance floor centre. DJ Matt Shepard 0400 111 222."


def _booking(db, space, name):
    contact = Contact(name=name, email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 8, 21),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _draft(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


# --- the edit form ------------------------------------------------------------


def test_a_save_onto_a_superseded_draft_is_refused(db, loft):
    """At the service boundary, where every writer passes."""
    booking = _booking(db, loft, "Superseded Save")
    v1 = _draft(db, booking)
    _draft(db, booking)  # supersedes it
    db.refresh(v1)
    assert v1.status == DocumentStatus.draft, "a superseded row keeps its status -- that is the trap"
    assert v1.is_current is False

    with pytest.raises(ValueError) as exc:
        documents_service.lock_draft_for_update(db, v1)

    assert "newer version has replaced it" in str(exc.value)
    assert f"v{v1.version}" in str(exc.value), "it should name the version being refused"


def test_typing_into_a_stale_edit_form_does_not_land_on_a_dead_row(db, loft, admin_client):
    """Through the real route, which is how it was found. No race: open the
    form, have anything regenerate, then save."""
    booking = _booking(db, loft, "Superseded Form")
    v1 = _draft(db, booking)
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{v1.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    v2 = _draft(db, booking)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{v1.id}/edit",
        data={"csrf_token": csrf, "content_expect": expect, "room_layout_notes": TYPED},
        follow_redirects=False,
    )

    assert response.status_code != 303, "the save was accepted onto a version nobody can read"
    assert response.status_code == 409
    db.refresh(v1)
    db.refresh(v2)
    assert v1.content.get("room_layout_notes") != TYPED, "their words landed on the dead row"
    assert v2.content.get("room_layout_notes") != TYPED, "and they did not silently move either"


def test_the_current_draft_still_saves_normally(db, loft, admin_client):
    """The guard must not cost the ordinary path anything."""
    booking = _booking(db, loft, "Current Saves")
    document = _draft(db, booking)
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={"csrf_token": csrf, "content_expect": expect, "room_layout_notes": TYPED},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(document)
    assert document.content["room_layout_notes"] == TYPED


# --- send ---------------------------------------------------------------------


def test_a_superseded_draft_cannot_be_sent(db, loft):
    """It flipped to `sent` and the link 410s, because the public route
    gates on is_current. Staff would have "sent" the client nothing."""
    booking = _booking(db, loft, "Superseded Send")
    v1 = _draft(db, booking)
    _draft(db, booking)
    db.refresh(v1)

    with pytest.raises(ValueError) as exc:
        documents_service.mark_sent(db, v1, actor="staff:aaron")

    assert "its link would not work" in str(exc.value)
    db.refresh(v1)
    assert v1.status == DocumentStatus.draft, "it was sent anyway"


def test_a_superseded_agreement_cannot_take_a_room_hold(db, loft):
    """Sending an agreement is half of what holds the date
    (booking.auto_hold_on_send). Doing that off the back of a document the
    client can never open is the part that reaches the calendar."""
    booking = _booking(db, loft, "Superseded Hold")
    from app.services.document_generation import generate_agreement_content

    v1 = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    db.refresh(v1)
    status_before = booking.status

    with pytest.raises(ValueError):
        documents_service.mark_sent(db, v1, actor="staff:aaron")

    db.refresh(booking)
    assert booking.status == status_before, "a dead agreement moved the booking"


def test_the_current_draft_still_sends(db, loft):
    booking = _booking(db, loft, "Current Sends")
    document = _draft(db, booking)

    sent = documents_service.mark_sent(db, document, actor="staff:aaron")

    assert sent.status == DocumentStatus.sent
