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
    booking.child_count = 3
    db.commit()

    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    from app.services.document_generation import format_date_long

    assert "The booking has changed since this version" in page
    note = page[page.index("The booking has changed since this version"):]
    # Under-18s and RSA come FIRST -- the floor needs them more than a room change.
    assert note.index("under-18s are now 3 (this version says 0)") < note.index("the date is now")
    # RSA is its own line, ABOVE the "changed" list -- it is a standing
    # fact about this version, not a change.
    assert "RSA applies to this booking" in page
    assert page.index("RSA applies to this booking") < page.index("The booking has changed since this version")
    assert "Special notes do not carry the RSA line" in page
    assert f"the date is now {format_date_long(dt.date(2026, 10, 10))}" in note, "the same form the run sheet prints"
    assert "adults are now 55 (this version says 40)" in note


def test_the_floor_screen_is_wired_to_the_new_fields():
    """The app is a single static page of string-built HTML; nothing else
    executes its JavaScript. Pin that the two new fields are read."""
    from pathlib import Path

    source = Path("app/templates/floor/floor.html").read_text(encoding="utf-8")
    assert "b.beo_approved" in source
    assert "b.beo_newer_unapproved" in source
    assert "Approved by the client" in source
    assert "Not yet approved by the client" in source
    assert "is being prepared" in source, "a Revise in flight is said on the sheet"
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


def test_a_sent_run_sheet_stays_on_the_floor_during_a_revise(client, db, loft, contact):
    """Aaron, 2026-09-10: "If the floor is working from a sent document
    and I start a revise, they should keep seeing what they had until the
    new version goes out, with a note that a newer one is coming."
    """
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    documents_service.revise(db, v1, actor="staff:aaron")

    detail = _detail(client, headers, booking)
    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers)
    pdf = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert detail["beo_ready"] is True, "the sent run sheet did not vanish when the Revise began"
    assert detail["beo_approved"] is False
    assert detail["beo_newer_unapproved"] == {"version": 2, "status": "draft"}
    assert page.status_code == 200
    assert "not yet approved by the client" in page.text
    assert "A newer version (v2, draft) is being prepared" in page.text
    assert "this is the last one sent" in page.text
    assert "BEO-v1.pdf" in pdf.headers["content-disposition"]


def test_the_floor_pdf_withholds_the_phone_and_the_money_like_the_screen(client, db, loft, contact, monkeypatch):
    """Aaron, 2026-09-10: "The floor team doesn't need the client's phone
    on a document that gets left on a bar, and the billing summary is a
    conversation for me, not them."
    """
    from app.api import staff_app
    from app.templating import templates

    captured = []

    def fake_pdf(html):
        captured.append(html)
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(staff_app, "render_html_to_pdf", fake_pdf)
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    assert contact.phone == "0400000000"
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")

    resp = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert resp.status_code == 200 and len(captured) == 1
    html = captured[0]
    assert "0400000000" not in html, "the client's phone is on the bar"
    assert "Billing Summary" not in html and "Total Paid" not in html and "Balance owing" not in html
    assert "fire pizzas" not in html, "kitchen notes still stay off the shareable copy"
    assert "floor-version-note" not in html, "the version note is the screen's, not the PDF's"
    # The client's own PDF is untouched: it keeps the billing summary.
    client_pdf = templates.get_template("document.html").render(document=v1, booking=booking, is_pdf=True)
    assert "Billing Summary" in client_pdf and "0400000000" in client_pdf


# --- the review of the follow-ups ------------------------------------------------------


def test_an_untouched_under_18_approval_gets_the_rsa_line_but_no_changed_heading(client, db, loft, contact):
    """The RSA warning is true of a booking nobody touched; printing it
    under 'The booking has changed' was a lie (review, 2026-09-10)."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact, child_count=3)
    _approved_v1(db, booking)

    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    assert "RSA applies to this booking" in page
    assert "The booking has changed since this version" not in page


def test_no_rsa_line_when_the_version_already_carries_it(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact, child_count=3)
    content = _beo_content(booking)
    content["special_notes"] = "Strict RSA applies; no alcohol to under-18s."
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    documents_service.sign(db, v1, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).text

    assert "RSA applies to this booking" not in page


def test_a_deleted_draft_does_not_blank_the_floor(client, db, loft, contact):
    """Revise, then delete the draft. This test used to record the broken
    state as pre-existing ("the deleted draft did not hand currency back")
    and proved only that the floor's own fallback covered for it. Since
    2026-09-11 deleting a Revise draft hands currency back, so the floor
    reaches the run sheet by the ordinary path and the client's link works
    again too -- which was the half nobody was covering.

    The floor's fallback stays in place as defence: a booking left with no
    current version by a delete from BEFORE that fix still needs it, until
    a stale draft is cleared off it."""
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    v2 = documents_service.revise(db, v1, actor="staff:aaron")
    documents_service.delete_draft(db, v2, actor="staff:aaron")
    db.refresh(v1)
    assert v1.is_current is True, "abandoning a Revise must hand currency back to the sent version"

    detail = _detail(client, headers, booking)

    assert detail["beo_ready"] is True
    assert detail["beo_newer_unapproved"] is None
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 200
    # And the thing the old behaviour broke: the client's own link.
    assert client.get(f"/d/{v1.access_token}").status_code == 200


def test_a_viewed_run_sheet_stays_during_a_revise(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    assert client.get(f"/d/{v1.access_token}").status_code == 200  # the client opens it: viewed
    db.refresh(v1)
    assert v1.status == DocumentStatus.viewed
    documents_service.revise(db, v1, actor="staff:aaron")

    detail = _detail(client, headers, booking)

    assert detail["beo_ready"] is True
    assert detail["beo_newer_unapproved"] == {"version": 2, "status": "draft"}


def test_two_sent_versions_then_a_draft_opens_the_newest_sent(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    v2 = documents_service.revise(db, v1, actor="staff:aaron")
    documents_service.mark_sent(db, v2, actor="staff:aaron")
    documents_service.revise(db, v2, actor="staff:aaron")

    detail = _detail(client, headers, booking)
    pdf = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert detail["beo_newer_unapproved"] == {"version": 3, "status": "draft"}
    assert "BEO-v2.pdf" in pdf.headers["content-disposition"]


def test_a_legacy_sent_row_never_stays_during_a_revise(client, db, loft, contact):
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    v2 = documents_service.create_new_version(db, booking, DocumentType.beo, _beo_content(booking), actor="test")
    v1.status = DocumentStatus.sent
    v1.is_legacy = True
    db.commit()
    assert v2.status == DocumentStatus.draft and v2.is_current

    detail = _detail(client, headers, booking)

    assert detail["beo_ready"] is False
    assert detail["beo_newer_unapproved"] is None
    assert client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers).status_code == 404


def test_the_floor_pdf_of_an_approved_version_says_so_and_still_withholds(client, db, loft, contact, monkeypatch):
    from app.api import staff_app

    captured = []
    monkeypatch.setattr(staff_app, "render_html_to_pdf", lambda html: captured.append(html) or b"%PDF-1.4 fake")
    headers = _login(client)
    booking = _confirmed_booking(db, loft, contact)
    content = _beo_content(booking)
    content["onsite_contact"] = None
    v1 = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    documents_service.mark_sent(db, v1, actor="test")
    documents_service.sign(db, v1, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    resp = client.get(f"/api/staff/bookings/{booking.id}/beo.pdf", headers=headers)

    assert resp.status_code == 200
    html = captured[0]
    assert "Approved by Caitlin Hobday" in html
    assert "0400000000" not in html and "Billing Summary" not in html
    assert "Onsite Contact:</span> —" in html, "an unfilled onsite contact is a dash on the bar copy, never the client's name"
