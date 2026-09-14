"""Checking your own work must not record the client as having read it.

Aaron, 2026-09-14: "The View -> button on the booking page links to the
client URL, so every time I check my own work it records my client as
having opened the document. That is corrupting data every day it stays
live, and it makes the one field that tells me whether a client has read
their Event Order useless."

WHAT WAS WRONG. `record_view` is called from exactly one place, the public
`GET /d/{token}` route, and it stamps the transition with the actor
"client (auto)". The booking page's own "View ->" button linked straight to
`/d/{access_token}` -- the client's URL. So a staff member opening it to
read the document they had just sent was recorded, indistinguishably, as
the client opening it.

THE FIX IS THE ROUTE THAT ALREADY EXISTS AND ALREADY SAYS WHY. The staff
preview route's docstring: "Reuses the exact template a client would see;
does not call record_view, since a staff read must never be mistaken for
the client having seen it." It has no status guard, so it serves a sent,
viewed or signed document as readily as a draft.

THE CLIENT STAMP IS DELIBERATELY LEFT WORKING. Aaron: "once it's fixed, I
want to know whether a client has opened the document, because that is
genuinely useful to me. Don't just remove the stamp." So `/d/{token}`
still records a view; only the staff door stops doing it.
"""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.document import DocumentStatus, DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking

BOOKING_PAGE = "app/templates/admin/booking_detail.html"


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _booking(db, loft, contact, name="Staff Read"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=20), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _sent_beo(db, booking):
    content = beo_proposals.fresh_beo_content(db, booking)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="test"
    )
    db.flush()
    documents_service.mark_sent(db, document, actor="staff:test@meantime.com.au")
    db.flush()
    return document


# --- the staff door ---------------------------------------------------------


def test_the_staff_read_leaves_no_client_view(admin_client, db, hamilton, loft, contact):
    """THE one. Open the document the way staff open it, and the client's
    record must be untouched."""
    booking = _booking(db, loft, contact, name="ZZSTAFFREAD Beo")
    document = _sent_beo(db, booking)
    assert document.status == DocumentStatus.sent
    assert document.viewed_at is None

    page = admin_client.get(
        f"/admin/hamilton/bookings/{booking.id}/documents/{document.id}/preview",
        follow_redirects=True,
    )

    assert page.status_code == 200, page.text
    db.refresh(document)
    assert document.status == DocumentStatus.sent, "a staff read moved the document to viewed"
    assert document.viewed_at is None, "a staff read stamped viewed_at"


def test_the_booking_page_sends_staff_to_the_preview_not_the_client_link(
    admin_client, db, hamilton, loft, contact
):
    """The assertion is on the rendered page, because the defect WAS a link
    target -- a fix nothing renders is no fix."""
    booking = _booking(db, loft, contact, name="ZZLINKTARGET Beo")
    document = _sent_beo(db, booking)

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)

    assert page.status_code == 200
    assert f"/documents/{document.id}/preview" in page.text, (
        "the staff View link does not point at the preview route"
    )
    assert f'href="/d/{document.access_token}"' not in page.text, (
        "the booking page still links staff straight at the client's URL"
    )


def test_no_view_link_on_the_booking_page_points_at_the_client_url():
    """Structural, because the two View links are in different branches of
    the template and a behavioural test only covers whichever one the
    fixture happens to build. The client's URL may still appear as the PDF
    link and inside Copy link -- those are the client's copy and the thing
    staff actually send, and neither records a view."""
    import pathlib

    markup = pathlib.Path(BOOKING_PAGE).read_text(encoding="utf-8")
    hrefs = re.findall(r'href="(/d/\{\{[^"]*)"', markup)
    viewing = [h for h in hrefs if not h.rstrip().endswith("/pdf")]

    assert viewing == [], f"a booking-page link opens the client's own document URL: {viewing}"


# --- the client stamp must survive -----------------------------------------


def test_a_real_client_open_still_records_a_view(db, hamilton, loft, contact):
    """Aaron asked for this explicitly: don't just remove the stamp. The
    public link is what a client actually opens, and it must still say so."""
    booking = _booking(db, loft, contact, name="ZZCLIENTVIEW Beo")
    document = _sent_beo(db, booking)

    try:
        resp = _client(db).get(f"/d/{document.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    db.refresh(document)
    assert document.status == DocumentStatus.viewed, "the client's own open no longer records"
    assert document.viewed_at is not None


def test_the_client_pdf_link_does_not_record_a_view(db, hamilton, loft, contact):
    """Recorded because it is the reason the PDF link beside View was left
    alone: /d/{token}/pdf renders without calling record_view. It also
    means a client who only ever downloads the PDF never registers as
    having viewed -- true before this change and unchanged by it."""
    booking = _booking(db, loft, contact, name="ZZCLIENTPDF Beo")
    document = _sent_beo(db, booking)

    try:
        resp = _client(db).get(f"/d/{document.access_token}/pdf")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    db.refresh(document)
    assert document.status == DocumentStatus.sent
    assert document.viewed_at is None


# --- the banner must not lie ------------------------------------------------


@pytest.mark.parametrize("send_it", [False, True])
def test_the_preview_banner_states_the_real_status(
    admin_client, db, hamilton, loft, contact, send_it
):
    """The banner said "this has not been sent to the client" whatever the
    status was. Staff now arrive here for SENT documents, so on exactly the
    documents they were checking it would have told them the opposite of
    the truth -- the mislabelled page this project keeps hitting."""
    booking = _booking(db, loft, contact, name=f"ZZBANNER {send_it}")
    content = beo_proposals.fresh_beo_content(db, booking)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="test"
    )
    db.flush()
    if send_it:
        documents_service.mark_sent(db, document, actor="staff:test@meantime.com.au")
        db.flush()

    page = admin_client.get(
        f"/admin/hamilton/bookings/{booking.id}/documents/{document.id}/preview",
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "Staff preview" in page.text
    if send_it:
        assert "has not been sent to the client" not in page.text, (
            "the banner claims a sent document was never sent"
        )
        assert "does not count as a client view" in page.text
    else:
        assert "has not been sent to the client" in page.text
