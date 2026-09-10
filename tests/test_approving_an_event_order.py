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
    assert 'name="accept_lock" value="yes" required' in page.text, "the browser half of the two-layer rule"
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


def test_approval_confirms_nothing_about_the_booking(client, db, loft, monkeypatch):
    """The agreement and the deposit confirm a booking. The thing that does
    that is booking_service.auto_confirm_if_ready, which sign() calls for an
    agreement -- pin that it is NOT called for an Event Order. (The first
    version of this test looked for an event sign() never writes, so it
    could not fail.)"""
    from app.services import booking as booking_service

    calls = []
    monkeypatch.setattr(booking_service, "auto_confirm_if_ready", lambda db_, b, **kw: calls.append(b.id))
    booking = _booking(db, loft, "Approve No Confirm")
    sent = _sent_beo(db, booking)
    status_before = booking.status

    client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "yes"})

    db.refresh(booking)
    assert booking.status == status_before
    assert calls == [], "approving an Event Order tried to confirm the booking"

    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")
    client.post(f"/d/{agreement.access_token}/sign", data={"signer_name": "Caitlin Hobday"})
    assert calls == [booking.id], "and signing the agreement still does"


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
    # AS TYPED. "caitlin hobday" is what they approved as, and a signature
    # prints what was signed -- recasing it is the admin page's business.
    assert "Approved by caitlin hobday" in page, "the signed-note should use the honest verb, as typed"
    assert "Approved by Caitlin Hobday" not in page, "a client surface recased the name they typed"
    # No badge assertion: the Event Order's screen view has no status pill
    # (the reference layout never had one) -- its Status SECTION is the
    # status, and that is asserted next.
    assert "Event Order approved by caitlin hobday" in page, "the Status line still said 'Awaiting Event Order approval'"
    assert ">signed<" not in page, "nothing on a run sheet should call an approval a signature"
    assert "Awaiting Event Order approval" not in page
    assert "Approve this Event Order" not in page, "the form must go once approved"

    pdf_html = templates.get_template("document.html").render(document=sent, booking=booking, is_pdf=True)
    assert "Approved by caitlin hobday" in pdf_html, "the PDF must print the name as typed, exactly"
    assert "Signed by" not in pdf_html


def test_a_new_version_is_never_generated_as_approved(db, loft):
    """REVERSED on review. The first version derived "Event Order approved"
    from has_approved_beo -- which runs while the OLD version is still
    current, so every unapproved replacement was born saying it had been
    approved. A new version is unapproved by definition; only the approved
    version's own render says otherwise."""
    booking = _booking(db, loft, "Approve Status Text")
    sent = _sent_beo(db, booking)
    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")
    assert has_approved_beo(db, booking) is True, "the old version is still current at this moment"

    text = _build_status_text(db, booking)

    assert "Awaiting Event Order approval" in text
    assert "Event Order approved" not in text, "a replacement was composed as already approved"


def test_the_unapproved_replacement_does_not_print_approved(client, db, loft):
    """End to end: approve, Revise, send the copy -- the copy must read as
    awaiting approval, not carry the old approval's sentence."""
    booking = _booking(db, loft, "Approve Replacement")
    sent = _sent_beo(db, booking)
    client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "yes"})
    draft = documents_service.revise(db, sent, actor="staff:aaron")
    documents_service.mark_sent(db, draft, actor="staff:aaron")

    page = client.get(f"/d/{draft.access_token}").text

    assert "Event Order approved" not in page, "the replacement claims an approval it never got"
    assert "Approve this Event Order" in page, "and it should be asking for one"


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


# --- the form is the CLIENT's, and nobody else's ------------------------------


def _floor_client(db, hamilton):
    """A logged-in floor user, so the floor app's own BEO route renders."""
    from app.services import staff_auth

    staff_auth.create_or_update_staff_user(
        db, email="floor.approve@meantime.com.au", name="Floor Approve", password="floorpassword1", role="floor"
    )
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    resp = c.post("/api/staff/login", json={"email": "floor.approve@meantime.com.au", "password": "floorpassword1"})
    assert resp.status_code == 200, resp.text
    return c, {"Authorization": f"Bearer {resp.json()['token']}"}


def test_the_staff_preview_never_shows_a_live_approve_or_sign_form(admin_client, db, loft):
    """The admin preview used to render the agreement's real Accept & Sign
    form, and this commit had added Approve beside it. Both post to
    /d/{token}/sign and record the signer as "client:<name>" from THAT
    request's IP -- a staff member could sign a contract, or approve a run
    sheet, in the client's name. The legacy-upload path is how a
    paper-signed agreement gets on record; this is not."""
    booking = _booking(db, loft, "Approve Staff Preview")
    beo = _sent_beo(db, booking)
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")

    beo_preview = admin_client.get(f"/admin/bookings/{booking.id}/documents/{beo.id}/preview").text
    agr_preview = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/preview").text

    assert "Approve this Event Order" not in beo_preview
    assert 'name="accept_lock"' not in beo_preview
    assert "Accept &amp; Sign" not in agr_preview
    assert "/sign" not in beo_preview and "/sign" not in agr_preview, "a live sign action on an internal page"


def test_the_floor_app_never_shows_the_approve_form(db, hamilton, loft):
    """A bartender's phone. It shows the run sheet; it must not offer to
    approve it in the client's name."""
    try:
        client, headers = _floor_client(db, hamilton)
        # test_staff_app's own helper: it already knows how to make a booking
        # the floor app will show, and guessing at change_status's signature
        # is how the first version of this test failed.
        from tests.test_staff_app import _confirmed_booking

        contact = Contact(name="Floor Approve Client", email="floor.approve.client@example.com")
        db.add(contact)
        db.flush()
        booking = _confirmed_booking(db, loft, contact, event_name="Approve Floor")
        beo = _sent_beo(db, booking)

        page = client.get(f"/api/staff/bookings/{booking.id}/beo", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200, page.text[:300]
    assert "Approve this Event Order" not in page.text
    assert 'name="accept_lock"' not in page.text
    assert "/sign" not in page.text


def test_the_client_still_gets_the_form(client, db, loft):
    """The gate must cost the real path nothing."""
    booking = _booking(db, loft, "Approve Client Still")
    beo = _sent_beo(db, booking)

    page = client.get(f"/d/{beo.access_token}").text

    assert "Approve this Event Order" in page
    assert f'action="/d/{beo.access_token}/sign"' in page


# --- refusals on the public POST are pages, not JSON ---------------------------


def test_approving_in_the_revise_window_gets_the_being_updated_card_not_json(client, db, loft):
    """Staff press Revise while the approval page is open; the click lands
    on a superseded version. The GET already showed the "being updated"
    card for exactly this window (Aaron's 2026-09-08 ruling); the POST
    answered {"detail": "This offer is no longer available..."} as JSON."""
    booking = _booking(db, loft, "Approve Revise Window")
    sent = _sent_beo(db, booking)
    documents_service.revise(db, sent, actor="staff:aaron")  # a draft copy; the old link is now dead

    resp = client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "yes"})

    assert resp.status_code == 410
    assert "text/html" in resp.headers["content-type"], "a client got raw JSON"
    assert "being updated" in resp.text, "the POST should say what the GET says"
    db.refresh(sent)
    assert sent.status == DocumentStatus.sent, "and nothing was signed"


def test_a_double_click_shows_the_approved_page_not_a_409(client, db, loft):
    booking = _booking(db, loft, "Approve Double Click")
    sent = _sent_beo(db, booking)
    data = {"signer_name": "Caitlin Hobday", "accept_lock": "yes"}
    first = client.post(f"/d/{sent.access_token}/sign", data=data, follow_redirects=False)
    second = client.post(f"/d/{sent.access_token}/sign", data=data, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303, "the second click should land on the approved page, not a JSON 409"
    db.refresh(sent)
    assert sent.signer_name == "Caitlin Hobday"


def test_the_tick_is_a_value_not_a_presence(client, db, loft):
    """"0", "no" and "off" are not acceptance. The form posts "yes"."""
    booking = _booking(db, loft, "Approve Tick Value")
    sent = _sent_beo(db, booking)

    resp = client.post(f"/d/{sent.access_token}/sign", data={"signer_name": "Caitlin Hobday", "accept_lock": "0"})

    assert resp.status_code == 422
    db.refresh(sent)
    assert sent.status == DocumentStatus.sent


def test_sign_refuses_a_superseded_row_under_the_lock(db, loft):
    """The route checks is_current before calling sign(); this is the same
    check under the row lock, for the Revise that lands between the two.
    Superseding never changes status, so the row is still `sent`."""
    booking = _booking(db, loft, "Approve Superseded Sign")
    sent = _sent_beo(db, booking)
    documents_service.revise(db, sent, actor="staff:aaron")
    db.refresh(sent)
    assert sent.status == DocumentStatus.sent and sent.is_current is False

    with pytest.raises(ValueError) as exc:
        documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    assert "newer version" in str(exc.value)


# --- the agreement is byte-for-byte what it was --------------------------------


def test_the_agreements_signed_note_prints_the_name_as_typed(client, db, loft):
    """Rule 4 of the review. The first approval commit put a recasing
    filter on the note the agreement shares, so a contract's on-screen
    "Signed by" stopped matching its own PDF. A signature is what was
    signed."""
    booking = _booking(db, loft, "Approve Agreement Verbatim")
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, agreement, actor="staff:test")
    client.post(f"/d/{agreement.access_token}/sign", data={"signer_name": "pat wilson"})

    page = client.get(f"/d/{agreement.access_token}").text

    assert "Signed by pat wilson on" in page
    assert "Signed by Pat Wilson" not in page


# --- superseding an approval is never silent ----------------------------------


def test_a_wizard_submission_over_an_approved_event_order_is_not_clean(db, loft, menu_items):
    """A wizard session is submitted once, so the real shape of this is:
    staff generate and send an Event Order by hand, the client approves it,
    and the wizard invite they were also holding gets submitted afterwards.
    The wizard rebuilds the Event Order from its answers, superseding the
    approved one -- the replacement goes out unapproved, and that must
    escalate, not auto-route as clean."""
    from tests.test_wizard_generation import _complete_all_steps, _make_booking, _pay_deposit
    from app.services import wizard as wizard_service

    booking = _make_booking(db, loft)
    _pay_deposit(db, booking)
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")
    approved = _sent_beo(db, booking)
    documents_service.sign(db, approved, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")
    _complete_all_steps(db, session, menu_items)

    session, result = wizard_service.submit_review(db, session, actor="wizard_client:test", final_notes=None)

    assert result.is_clean is False, "an approved Event Order was replaced and the system called it clean"
    assert any("approved by Caitlin Hobday" in item for item in result.outstanding_items), result.outstanding_items
    db.refresh(approved)
    assert approved.is_current is False and result.document.status == DocumentStatus.draft


def test_regenerate_asks_before_setting_an_approval_aside(admin_client, db, loft):
    """One-click Regenerate went through the loss screen only for
    hand-edited content; an approval is not content, so it was set aside
    with no signal. The button now confirms, naming the approver."""
    booking = _booking(db, loft, "Approve Regenerate Confirm")
    sent = _sent_beo(db, booking)

    before = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "was approved by" not in before, "no confirm while it is merely sent"

    documents_service.sign(db, sent, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")
    after = admin_client.get(f"/admin/bookings/{booking.id}").text

    assert "was approved by Caitlin Hobday" in after
    assert "sets that approval aside" in after
