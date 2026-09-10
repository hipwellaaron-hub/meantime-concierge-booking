"""A client approves an Event Order at its link.

Aaron's ruling, 2026-09-10: "100%, they need to be approved. Type in their
name, and accepting the date, accepting that this locks it." Three things
at once -- the name, the event date as printed, and that approval locks the
version -- and the third is enforced on the server, not only by the
checkbox.

What approval is NOT: a change to the booking. The agreement and the
deposit confirm a booking; this records that the run sheet was read and
accepted, and tells the venue so.

What "locks it" means here, and the one deliberate difference from the
signed-agreement rule: the approved VERSION is never touched, but staff can
still Revise it -- an Event Order changes right up to the day, and Revise
is the safe way to change one. The copy starts as a draft, goes out again,
and needs the client's approval again. The trail records that an approved
version was set aside, so nobody treats the replacement as agreed.
"""

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import BookingEvent, Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking, has_approved_beo
from app.services.document_generation import generate_agreement_content, generate_beo_content
from app.services.wizard_generation import _build_status_text


def _booking(db, space, name="Approve"):
    contact = Contact(name="Approving Client", email=f"approve.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _sent_beo(db, booking):
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    return documents_service.mark_sent(db, document, actor="staff:test")


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- the client's screen ------------------------------------------------------


def test_a_sent_event_order_offers_approval(client, db, loft):
    booking = _booking(db, loft, "Approve Offered")
    sent = _sent_beo(db, booking)

    page = client.get(f"/d/{sent.access_token}")

    assert page.status_code == 200
    assert "Approve this Event Order" in page.text
    assert 'name="accept_lock"' in page.text
    assert "locks this Event Order" in page.text, "the lock has to be said before they tick it"
    assert "including the event date" in page.text
    assert "Accept &amp; Sign" not in page.text, "that is the agreement's wording, not this document's"


def test_an_agreement_still_signs_and_never_shows_the_approval_form(client, db, loft):
    booking = _booking(db, loft, "Approve Agreement Unchanged")
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")

    page = client.get(f"/d/{agreement.access_token}")

    assert "Accept &amp; Sign" in page.text
    assert "Approve this Event Order" not in page.text
    assert 'name="accept_lock"' not in page.text


# --- approving --------------------------------------------------------------


def test_approving_records_who_when_and_from_where(client, db, loft):
    booking = _booking(db, loft, "Approve Records")
    sent = _sent_beo(db, booking)

    resp = client.post(
        f"/d/{sent.access_token}/sign",
        data={"signer_name": "caitlin hobday", "accept_lock": "yes"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(sent)
    assert sent.status == DocumentStatus.signed
    assert sent.signer_name == "caitlin hobday"
    assert sent.signed_at is not None
    assert sent.signer_ip is not None
    assert has_approved_beo(db, booking) is True


def test_the_lock_must_be_accepted_on_the_server_not_only_in_the_browser(client, db, loft):
    """The checkbox is `required` in the HTML. A form field can be posted
    without the page, so the server refuses too -- otherwise "accepting
    that this locks it" is a sentence on a screen, not a rule."""
    booking = _booking(db, loft, "Approve No Tick")
    sent = _sent_beo(db, booking)

    resp = client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday"})

    assert resp.status_code == 422
    assert "locks this Event Order" in resp.text
    db.refresh(sent)
    assert sent.status == DocumentStatus.sent, "approved without accepting the lock"
    assert has_approved_beo(db, booking) is False


def test_an_agreement_does_not_need_the_tick(client, db, loft):
    """The requirement is the Event Order's. Signing a contract is the
    existing flow and must not gain a field its form does not post."""
    booking = _booking(db, loft, "Approve Agreement No Tick")
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")

    resp = client.post(f"/d/{agreement.access_token}/sign", data={"signer_name": "Pat Wilson"}, follow_redirects=False)

    assert resp.status_code == 303
    db.refresh(agreement)
    assert agreement.status == DocumentStatus.signed


def test_approval_confirms_nothing_about_the_booking(client, db, loft):
    """The agreement and the deposit confirm a booking. An approved run
    sheet must not move status, and must not fire the agreement's alert."""
    booking = _booking(db, loft, "Approve No Confirm")
    sent = _sent_beo(db, booking)
    status_before = booking.status

    client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "yes"})

    db.refresh(booking)
    assert booking.status == status_before
    kinds = {e.event_type for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all()}
    assert "agreement_signed" not in kinds


# --- what the approved document then says --------------------------------------


def test_the_approved_document_reads_approved_everywhere(client, db, loft):
    """Status badge, the signed-note, the run sheet's own Status line -- and
    the PDF. "Signed" is the agreement's verb; a run sheet is approved."""
    from app.templating import templates

    booking = _booking(db, loft, "Approve Reads")
    sent = _sent_beo(db, booking)
    client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "caitlin hobday", "accept_lock": "yes"})
    db.refresh(sent)

    page = client.get(f"/d/{sent.access_token}").text
    assert "Approved by Caitlin Hobday" in page, "the signed-note should use the honest verb and recase the name"
    # No badge assertion: the Event Order's screen view has no status pill
    # (the reference layout never had one) -- its Status SECTION is the
    # status, and that is asserted next.
    assert "Event Order approved by Caitlin Hobday" in page, "the Status line still said 'Awaiting Event Order approval'"
    assert ">signed<" not in page, "nothing on a run sheet should call an approval a signature"
    assert "Awaiting Event Order approval" not in page
    assert "Approve this Event Order" not in page, "the form must go once approved"

    pdf_html = templates.get_template("document.html").render(document=sent, booking=booking, is_pdf=True)
    assert "Approved by caitlin hobday" in pdf_html or "Approved by Caitlin Hobday" in pdf_html
    assert "Signed by" not in pdf_html


def test_status_text_at_generation_derives_approval(db, loft):
    """The suffix "Awaiting Event Order approval" was fixed text that could
    never come true. Now it is derived, like the two facts before it."""
    booking = _booking(db, loft, "Approve Status Text")
    assert "Awaiting Event Order approval" in _build_status_text(db, booking)

    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    text = _build_status_text(db, booking)
    assert "Event Order approved" in text
    assert "Awaiting Event Order approval" not in text
    assert "Awaiting final invoice payment" in text


# --- the lock, and what it does not lock ------------------------------------------


def test_an_approved_version_cannot_be_edited(admin_client, db, loft):
    booking = _booking(db, loft, "Approve Locked")
    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    resp = admin_client.get(f"/admin/bookings/{booking.id}/documents/{sent.id}/edit")

    assert resp.status_code in (404, 409), "an approved version was editable in place"


def test_revise_still_works_and_the_copy_needs_approving_again(admin_client, db, loft):
    """THE deliberate difference from a signed agreement. An Event Order
    changes until the day; Revise is the safe change. The approved version
    is untouched, the copy is a draft, and it is unapproved."""
    booking = _booking(db, loft, "Approve Then Revise")
    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    draft = documents_service.revise(db, sent, actor="staff:aaron")

    assert draft.status == DocumentStatus.draft
    assert draft.signer_name is None and draft.signed_at is None
    db.refresh(sent)
    assert sent.status == DocumentStatus.signed, "the approved version was altered"
    assert sent.is_current is False
    assert has_approved_beo(db, booking) is False, "a superseded approval must not carry to the new version"


def test_a_signed_agreement_is_still_refused_revise(db, loft):
    """Unchanged, and the contrast that makes the difference above a
    decision rather than an accident."""
    booking = _booking(db, loft, "Approve Agreement Refused")
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")
    documents_service.sign(db, agreement, signer_name="Pat Wilson", signer_ip="10.0.0.1")

    with pytest.raises(ValueError) as exc:
        documents_service.revise(db, agreement, actor="staff:aaron")

    assert "signed" in str(exc.value)


def test_superseding_an_approved_version_is_written_to_the_trail(db, loft):
    """Nothing blocks it. But the trail must say an approval was set aside,
    because the replacement goes out unapproved and the floor must not
    treat it as agreed."""
    booking = _booking(db, loft, "Approve Superseded")
    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    documents_service.revise(db, sent, actor="staff:aaron")

    events = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="beo_approval_superseded").all()
    assert len(events) == 1, [e.event_type for e in db.query(BookingEvent).filter_by(booking_id=booking.id)]
    assert "Caitlin Hobday had approved" in events[0].new_value
    assert "needs approving again" in events[0].new_value
    assert events[0].old_value == str(sent.version)


# --- the booking page ---------------------------------------------------------


def test_the_booking_page_says_who_approved_it(admin_client, db, loft):
    booking = _booking(db, loft, "Approve Booking Page")
    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="caitlin hobday", signer_ip="10.0.0.1")

    page = admin_client.get(f"/admin/bookings/{booking.id}").text

    beo_row = page[page.index("<td>BEO</td>"):]
    assert "Approved by Caitlin Hobday" in beo_row
    assert ">approved<" in beo_row, "the row badge should read approved, not signed"


# --- the venue is told -----------------------------------------------------------


def test_approving_alerts_the_venue_and_signing_an_agreement_does_not_cross_wires(client, db, loft, monkeypatch):
    """Gmail is not configured in tests, so the real alert no-ops -- which
    means a mutation deleting the call would survive every other test here.
    Pin the CALL, with its arguments, and pin that an agreement signature
    still goes to its own alert and not this one."""
    from app.services import notifications

    approved = []
    signed = []
    monkeypatch.setattr(notifications, "notify_beo_approved", lambda booking, **kw: approved.append((booking.id, kw)))
    monkeypatch.setattr(notifications, "notify_agreement_signed", lambda booking, **kw: signed.append(booking.id))

    booking = _booking(db, loft, "Approve Alert")
    beo = _sent_beo(db, booking)
    client.post(f"/d/{beo.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "yes"})

    assert approved == [(booking.id, {"signer_name": "Caitlin Hobday", "version": beo.version})]
    assert signed == [], "an Event Order approval fired the agreement alert"

    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")
    client.post(f"/d/{agreement.access_token}/sign", data={"signer_name": "Caitlin Hobday"})

    assert signed == [booking.id]
    assert len(approved) == 1, "an agreement signature fired the Event Order alert"
