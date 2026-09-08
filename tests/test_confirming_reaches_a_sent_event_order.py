"""Confirming setup access on an Event Order the client already holds.

_refresh_draft_beo_timeline is deliberately a no-op on anything but a
draft: a sent document is never mutated under a client. Its docstring says
so, and says the sent one "goes on saying requested until it is
regenerated".

That sentence was written before Revise existed, and Regenerate is the
button that rebuilds from the booking and can discard hand-entered content
-- on HAM-20260912-2R11Q that would put a hand-typed $140 Custom Vegan
Platter through the loss screen for the sake of two words on a run sheet.

There is a safer route and it needs no new code, because the edit form's
save rebuilds event_timeline from the booking through the same builder
generation uses (admin_bookings.save_document_edit). So:

    Confirm  ->  Revise  ->  open the edit form and Save  ->  Send

Both copies of the line -- the run-sheet bullet and the legacy
`event_timeline.notes` string -- live inside that one rebuilt dict, so a
save with no edits at all flips both. This pins that, so the route stays
open and staff are never pushed to Regenerate for a wording change.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

PENDING = "requested, pending confirmation"


def _booking_wanting_early_access(db, space, name="Setup Access"):
    contact = Contact(name=name, email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 4, 17),
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name=name,
        event_type="birthday", adult_count=45, child_count=0, notes=None, actor="test",
    )
    booking.setup_access_time = dt.time(14, 0)
    booking.setup_access_confirmed = False
    db.flush()
    return booking


def _timeline_text(document) -> str:
    timeline = document.content["event_timeline"]
    return " ".join(timeline.get("bullets") or []) + " " + (timeline.get("notes") or "")


def _sent_beo(db, booking):
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    return documents_service.mark_sent(db, document, actor="staff:test")


def test_a_sent_event_order_is_not_rewritten_under_the_client(admin_client, db, loft):
    """The documented behaviour, pinned so it stays deliberate: confirming
    changes the booking and leaves the sent document exactly as the client
    has it."""
    booking = _booking_wanting_early_access(db, loft, "Setup Sent")
    sent = _sent_beo(db, booking)
    assert PENDING in _timeline_text(sent)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"',
        admin_client.get(f"/admin/bookings/{booking.id}").text,
    ).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(booking)
    db.refresh(sent)
    assert booking.setup_access_confirmed is True, "the booking fact was not recorded"
    assert PENDING in _timeline_text(sent), "a document the client holds was rewritten"
    assert sent.status == DocumentStatus.sent


def test_revise_then_save_flips_it_without_a_regenerate(admin_client, db, loft):
    """The safe route, end to end. No edits are typed: the save alone
    rebuilds the timeline from the booking."""
    booking = _booking_wanting_early_access(db, loft, "Setup Revise")
    sent = _sent_beo(db, booking)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"',
        admin_client.get(f"/admin/bookings/{booking.id}").text,
    ).group(1)
    admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf}, follow_redirects=False,
    )

    revised = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{sent.id}/revise",
        data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert revised.status_code == 303
    edit_url = revised.headers["location"]

    form = admin_client.get(edit_url)
    assert form.status_code == 200
    saved = admin_client.post(
        edit_url,
        data={
            "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1),
            "content_expect": re.search(r'name="content_expect" value="([^"]*)"', form.text).group(1),
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303, saved.text
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)
    text = _timeline_text(draft)
    assert PENDING not in text, "the run sheet still says pending after a save"
    assert "Setup access from 2:00pm (confirmed)" in text
    # BOTH copies: the bullet above and the legacy notes string.
    assert "(confirmed)" in (draft.content["event_timeline"].get("notes") or "")
    # And the version the client held is untouched.
    db.refresh(sent)
    assert PENDING in _timeline_text(sent)


def test_the_draft_case_still_refreshes_on_confirm_alone(admin_client, db, loft):
    """Unchanged, and the reason the two-step above is only needed for a
    sent document: on a draft, confirming is enough by itself."""
    booking = _booking_wanting_early_access(db, loft, "Setup Draft")
    draft = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    assert PENDING in _timeline_text(draft)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"',
        admin_client.get(f"/admin/bookings/{booking.id}").text,
    ).group(1)

    admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf}, follow_redirects=False,
    )

    db.refresh(draft)
    assert PENDING not in _timeline_text(draft)
    assert "(confirmed)" in _timeline_text(draft)
