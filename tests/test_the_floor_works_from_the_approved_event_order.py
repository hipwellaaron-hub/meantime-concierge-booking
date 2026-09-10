"""Meantime Floor works from the Event Order the client APPROVED.

Aaron, 2026-09-10: "The floor should be working from what the client
actually agreed to, not the latest draft. If a revise is sitting
unapproved, the team should see the approved one and know a newer version
exists rather than working from something the client hasn't seen."

Before this the floor opened whatever version was current and not a
draft -- so the moment staff pressed Revise and Send, a bartender's phone
showed a run sheet the client had never approved, with nothing on it to
say so.
"""

import pytest

from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from tests.test_staff_app import _beo_content, _confirmed_booking, _login, client, floor_user  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("floor_user")


def _approved_v1(db, booking):
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    documents_service.sign(db, v1, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")
    return v1


def _detail(client, headers, booking) -> dict:
    resp = client.get(f"/api/staff/bookings/{booking.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_floor_opens_the_approved_version_not_the_unapproved_revision(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = _approved_v1(db, booking)
    v2 = documents_service.revise(db, v1, actor="staff:aaron")
    documents_service.mark_sent(db, v2, actor="staff:aaron")
    db.refresh(v1)
    assert v1.is_current is False and v2.is_current is True, "the revision is current; the approval is not"

    detail = _detail(client, headers, booking)
    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers)
    pdf = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert detail["beo_ready"] is True
    assert detail["beo_approved"] is True
    assert detail["beo_newer_unapproved"] == {"version": 2, "status": "sent"}
    assert page.status_code == 200
    assert "Approved by the client" in page.text
    assert "v1, Caitlin Hobday" in page.text
    assert "A newer version (v2, sent) exists" in page.text
    assert "work from this one" in page.text
    assert "BEO-v1.pdf" in pdf.headers["content-disposition"], "the shareable copy must be the approved version too"
    # The note is the FLOOR's. The client's own page of the revision must
    # not grow a line about versions and approvals.
    client_page = client.get(f"/d/{v2.access_token}").text
    assert "floor-version-note" not in client_page
    assert "Approved by the client" not in client_page


def test_a_draft_revision_over_an_approval_still_opens_the_approved_one(client, db, loft, contact):
    """Revise without Send: the current version is a draft the client has
    not even been sent. The old rule answered 404 here ("no finalised
    BEO") the moment staff pressed Revise -- the approved run sheet
    vanished from every phone in the venue until the draft was re-sent."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = _approved_v1(db, booking)
    documents_service.revise(db, v1, actor="staff:aaron")

    detail = _detail(client, headers, booking)

    assert detail["beo_ready"] is True, "the approved version did not vanish because a draft exists"
    assert detail["beo_approved"] is True
    assert detail["beo_newer_unapproved"] == {"version": 2, "status": "draft"}
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 200


def test_a_newer_approval_replaces_the_older_one(client, admin_client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = _approved_v1(db, booking)
    v2 = documents_service.revise(db, v1, actor="staff:aaron")
    documents_service.mark_sent(db, v2, actor="staff:aaron")
    documents_service.sign(db, v2, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    detail = _detail(client, headers, booking)
    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    assert detail["beo_approved"] is True
    assert detail["beo_newer_unapproved"] is None
    assert "v2, Caitlin Hobday" in page
    assert "A newer version" not in page
    # The client's own page of the approved version carries no floor note,
    # and neither does the admin preview, which shares the template branch.
    client_page = client.get(f"/d/{v2.access_token}").text
    assert "floor-version-note" not in client_page
    preview = admin_client.get(f"/admin/bookings/{booking.id}/documents/{v2.id}/preview").text
    assert "floor-version-note" not in preview


def test_with_no_approval_anywhere_the_floor_is_what_it_was(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    assert _detail(client, headers, booking)["beo_ready"] is False, "a draft is still not a finalised BEO"

    documents_service.mark_sent(db, v1, actor="test")
    detail = _detail(client, headers, booking)
    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    assert detail["beo_ready"] is True
    assert detail["beo_approved"] is False
    assert detail["beo_newer_unapproved"] is None
    assert "not yet approved by the client" in page
    assert "Approved by the client" not in page


def test_a_legacy_signed_record_is_never_the_working_version(client, db, loft, contact):
    """A migrated iVvy record carries status signed and placeholder
    content. It must never outrank the real, current run sheet just
    because the real one is unapproved.

    No writer in the repo sets is_legacy on an Event Order (the importer
    and the legacy upload create agreements only), and create_new_version
    refuses to build over a legacy record -- so this state is unreachable
    today and the rows are written directly to exercise the defensive
    path, which exists so a future importer cannot put a placeholder on
    a phone under a green tick."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    v2 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    v1.status = DocumentStatus.signed
    v1.is_legacy = True
    v1.signer_name = "Migrated from iVvy"
    db.commit()
    documents_service.mark_sent(db, v2, actor="test")

    detail = _detail(client, headers, booking)
    pdf = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert detail["beo_approved"] is False
    assert detail["beo_newer_unapproved"] is None
    assert "BEO-v2.pdf" in pdf.headers["content-disposition"]


def test_version_rows_are_narrow_and_newest_first(db, loft, contact):
    """The floor list asks once per booking; it must not load every
    version's content to answer three booleans."""
    booking = _confirmed_booking(db, loft, contact)
    v1 = _approved_v1(db, booking)
    v2 = documents_service.revise(db, v1, actor="staff:aaron")

    rows = documents_service.version_rows(db, booking.id, DocumentType.beo)

    assert [r.version for r in rows] == [2, 1]
    assert [r.is_current for r in rows] == [True, False]
    assert rows[1].status == DocumentStatus.signed and rows[1].id == v1.id and rows[0].id == v2.id
    assert set(rows[0]._fields) == {"id", "version", "status", "is_current", "is_legacy"}, "no content column"


def test_a_lone_legacy_signed_row_is_not_a_working_version(client, db, loft, contact):
    """The floor was the one surface still rendering a legacy placeholder
    (the admin preview refuses it, the public link 404s), and with the
    approval build it would have done so under a green tick."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    v1.status = DocumentStatus.signed
    v1.is_legacy = True
    v1.signer_name = "Migrated from iVvy"
    db.commit()

    detail = _detail(client, headers, booking)

    assert detail["beo_ready"] is False
    assert detail["beo_approved"] is False
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 404


def test_the_floor_is_told_what_changed_on_the_booking_since_the_approved_version(client, db, loft, contact):
    """The header band reads the live name and rooms; the rest of the page
    is the snapshot. A date or guest change after approval was invisible."""
    import datetime as dt

    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    _approved_v1(db, booking)
    before = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text
    assert "The booking has changed since this version" not in before

    booking.event_date = dt.date(2026, 10, 10)
    booking.adult_count = 55
    db.commit()

    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    from app.services.document_generation import format_date_long

    assert "The booking has changed since this version" in page
    assert f"the date is now {format_date_long(dt.date(2026, 10, 10))}" in page, "the same form the run sheet prints"
    assert "guests are now 55 adults and 0 under 18" in page


def test_the_floor_screen_is_wired_to_the_new_fields():
    """The app is a single static page of string-built HTML; nothing else
    executes its JavaScript. Pin that the two new fields are read."""
    from pathlib import Path

    source = Path("app/templates/floor/floor.html").read_text(encoding="utf-8")
    assert "b.beo_approved" in source
    assert "b.beo_newer_unapproved" in source
    assert "Approved by the client" in source
    assert "Not yet approved by the client" in source
    # Approved is GREEN, the same as PAID -- the colour the team already reads as done.
    assert 'class="pill beo approved"' in source
    # Server strings reach innerHTML only through esc().
    assert "esc(b.event_name)" in source
    assert "'<h2>' + esc(b.event_name)" in source
    assert " + b.event_name + " not in source, "an unescaped event name reached innerHTML"
    # Approved and PAID share one colour: compare the two rule bodies,
    # not their literal text, so a reformat or a token move cannot fail this.
    import re

    def rule(selector):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", source)
        assert m, selector
        return " ".join(m.group(1).split())

    assert rule(".pill.beo.approved") == rule(".pill.paid"), "if PAID changes colour, approved follows it"


def test_regenerate_over_an_approval_leaves_the_floor_on_the_approved_version(client, db, loft, contact):
    """Regenerate (not Revise): both admin generate routes end in
    create_new_version with fresh content, which supersedes the approved
    row exactly as a Revise does. The floor must not follow it."""
    from app.services.document_generation import generate_beo_content

    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    _approved_v1(db, booking)
    documents_service.create_new_version(db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:aaron")

    detail = _detail(client, headers, booking)
    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    assert detail["beo_approved"] is True
    assert detail["beo_newer_unapproved"] == {"version": 2, "status": "draft"}
    assert "v1, Caitlin Hobday" in page
