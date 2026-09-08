"""Changing one word of a sent document without rebuilding it.

The edit form refuses anything that is not a draft, so until now the only
lever on a sent Event Order was Regenerate -- which rebuilds from the
booking and destroys whatever was typed. That is how HAM-20260912-2R11Q
lost a page of hand-entered content on 2026-09-07: saved at 23:41,
regenerated at 23:52, gone. Every guard built since has been a fence around
that path rather than a way to avoid needing it.

Revise copies the CURRENT content forward into a new draft. Nothing is
rebuilt, so there is nothing to destroy -- losses() against a copy of
itself is empty by construction, not because a guard did its job.

What it costs the client is what Regenerate already costs them: the public
route gates on is_current, so their link dies the moment a new version
exists. It does NOT rewrite what they are holding underneath them.

Aaron's two rulings, 2026-09-08:
  - a SIGNED agreement is refused outright. It is the client's evidence of
    what they agreed to; changing one means a new agreement they sign
    again, not a quiet supersession at 11pm;
  - the window between Revise and Send is made VISIBLE -- named on the
    booking page, and a client who opens the dead link in that window is
    told the document is being updated rather than that their link is gone.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.document import DocumentStatus, DocumentType
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content, generate_beo_content

TYPED = "Rounds of 8, dance floor centre. DJ Matt Shepard 0400 111 222."
ALLERGY = "1x severe nut allergy (table 4). Kitchen briefed."


def _booking(db, space, name="Revise"):
    contact = Contact(name="Revise Client", email=f"revise.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _sent_beo(db, booking, **overrides):
    content = generate_beo_content(booking)
    content.update(overrides)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )
    return documents_service.mark_sent(db, document, actor="staff:test")


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


# --- what it is for -----------------------------------------------------------


def test_it_carries_the_typed_content_forward_untouched(db, loft):
    """The whole point. A regenerate would have rebuilt these from the
    booking and lost both."""
    booking = _booking(db, loft)
    sent = _sent_beo(db, booking, room_layout_notes=TYPED, dietaries=ALLERGY)

    draft = documents_service.revise(db, sent, actor="staff:aaron")

    assert draft.status == DocumentStatus.draft
    assert draft.version == sent.version + 1
    assert draft.content["room_layout_notes"] == TYPED
    assert draft.content["dietaries"] == ALLERGY


def test_there_is_nothing_for_the_regenerate_guard_to_find(db, loft):
    """Empty by construction rather than by a guard doing its job: the new
    version IS the old content."""
    booking = _booking(db, loft, "Revise Nothing Lost")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED, dietaries=ALLERGY)

    draft = documents_service.revise(db, sent, actor="staff:aaron")

    assert dr.losses(db, sent, draft.content) == []


def test_the_new_draft_is_editable_where_the_sent_one_was_not(admin_client, db, loft):
    booking = _booking(db, loft, "Revise Editable")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)

    refused = admin_client.get(f"/admin/bookings/{booking.id}/documents/{sent.id}/edit")
    assert refused.status_code in (404, 409), "a sent document was editable"

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{sent.id}/revise",
        data={"csrf_token": _csrf(admin_client, booking.id)},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    # Straight to the edit form -- somebody clicking Revise is mid-sentence.
    assert "/edit" in resp.headers["location"]
    editable = admin_client.get(resp.headers["location"])
    assert editable.status_code == 200
    assert TYPED in editable.text, "their words are not in the form"


def test_the_sent_version_is_left_exactly_as_the_client_has_it(db, loft):
    """The claim that matters: revising does not reach back into what was
    already sent. The old row keeps its content, its status and its token,
    so the only thing that changes for the client is that the link stops
    resolving -- which is what a new version has always done.

    Deliberately NOT an object-identity assertion. create_new_version
    commits and refreshes, so the two contents are separate objects however
    the dict was built; asserting that would be testing SQLAlchemy.
    """
    booking = _booking(db, loft, "Revise Copy")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    before = sent.content["room_layout_notes"]
    token = sent.access_token

    documents_service.revise(db, sent, actor="staff:aaron")

    db.refresh(sent)
    assert sent.content["room_layout_notes"] == before
    assert sent.status == DocumentStatus.sent, "the sent version was reopened rather than copied"
    assert sent.access_token == token
    assert sent.is_current is False, "but it is no longer the one that resolves"


# --- the audit trail ----------------------------------------------------------


def test_the_trail_says_it_was_revised_not_regenerated(db, loft):
    """document_created alone cannot tell the two apart, and
    document_regenerated is only written when a regenerate actually lost
    something -- so a revise would otherwise look like a lossless
    regenerate."""
    from app.models import BookingEvent

    booking = _booking(db, loft, "Revise Trail")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)

    documents_service.revise(db, sent, actor="staff:aaron")

    events = db.query(BookingEvent).filter_by(
        booking_id=booking.id, event_type="document_revised"
    ).all()
    assert len(events) == 1, events
    assert events[0].old_value == str(sent.version)
    assert "copied forward" in events[0].new_value
    assert events[0].actor == "staff:aaron"


# --- Aaron's first ruling: signed agreements ----------------------------------


def test_a_signed_agreement_cannot_be_revised(db, loft):
    """The client's evidence of what they agreed to. Changing one means a
    new agreement they sign again."""
    booking = _booking(db, loft, "Revise Signed")
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, document, actor="staff:test")
    documents_service.sign(db, document, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    with pytest.raises(ValueError) as exc:
        documents_service.revise(db, document, actor="staff:aaron")

    assert "signed" in str(exc.value)
    assert "sign instead" in str(exc.value), "it has to say what to do instead of just refusing"


def test_the_signed_agreement_has_no_revise_button(admin_client, db, loft):
    """Not offered at all, so it cannot be pressed by accident at 11pm."""
    booking = _booking(db, loft, "Revise No Button")
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, document, actor="staff:test")
    documents_service.sign(db, document, signer_name="Caitlin Hobday", signer_ip="10.0.0.1")

    page = admin_client.get(f"/admin/bookings/{booking.id}")

    assert f"/documents/{document.id}/revise" not in page.text


def test_a_sent_but_unsigned_agreement_can_still_be_revised(db, loft):
    booking = _booking(db, loft, "Revise Unsigned")
    document = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, document, actor="staff:test")

    draft = documents_service.revise(db, document, actor="staff:aaron")

    assert draft.status == DocumentStatus.draft


def test_a_draft_is_not_revised_because_it_is_already_editable(db, loft):
    booking = _booking(db, loft, "Revise Draft")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )

    with pytest.raises(ValueError) as exc:
        documents_service.revise(db, document, actor="staff:aaron")

    assert "edited directly" in str(exc.value)


def test_a_legacy_document_is_refused(db, loft):
    booking = _booking(db, loft, "Revise Legacy")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    documents_service.mark_sent(db, document, actor="staff:test")
    document.is_legacy = True
    db.flush()

    with pytest.raises(ValueError) as exc:
        documents_service.revise(db, document, actor="staff:aaron")

    assert "legacy" in str(exc.value)


# --- and only the version that is actually current ----------------------------


def test_a_superseded_version_cannot_be_revised(db, loft):
    """The hole in the first cut of Revise, and the same class of defect it
    was built to close: content disappearing without a word.

    Superseding does not change a row's STATUS, so v1 of a twice-sent Event
    Order is still `sent` and satisfied every other check here. Revising it
    copied v1's content forward as v3 and discarded v2's -- proved by
    running it before this refusal existed.
    """
    booking = _booking(db, loft, "Superseded")
    v1 = _sent_beo(db, booking, room_layout_notes="Long tables, no dance floor")
    v2 = _sent_beo(db, booking, room_layout_notes=TYPED)
    assert v1.status == DocumentStatus.sent, "a superseded row keeps its status -- that is the trap"

    with pytest.raises(ValueError) as exc:
        documents_service.revise(db, v1, actor="staff:aaron")

    assert "no longer the current version" in str(exc.value)
    assert f"v{v2.version} is" in str(exc.value), "it should name the version to revise instead"
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.id == v2.id, "an older version was copied forward over the newer one"
    assert current.content["room_layout_notes"] == TYPED


def test_the_back_button_cannot_revise_the_version_it_just_replaced(admin_client, db, loft):
    """No race is needed to reach it. Revise, land on the edit form, press
    Back: the cached booking page still offers Revise on the version that
    was just superseded, and the second click threw away the draft the
    first click had made."""
    booking = _booking(db, loft, "Revise Back Button")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    csrf = _csrf(admin_client, booking.id)

    first = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{sent.id}/revise",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert first.status_code == 303
    draft_id = first.headers["location"].rsplit("/", 2)[1]

    again = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{sent.id}/revise",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert again.status_code == 409, "the stale page revised a superseded version"
    # The MESSAGE, not just the status. Refusing it as "already a draft"
    # would also be a 409 and would mean the request had been quietly
    # redirected onto a different version -- which is not the same as
    # telling the staff member their page is stale.
    assert "no longer the current version" in again.json()["detail"]
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert str(current.id) == draft_id, "the draft made by the first click was discarded"


# --- Aaron's second ruling: the window is visible -----------------------------


def test_the_booking_page_says_the_client_has_no_working_link(admin_client, db, loft):
    booking = _booking(db, loft, "Revise Window")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)

    before = admin_client.get(f"/admin/bookings/{booking.id}")
    assert "no working link" not in before.text, "said before anything was revised"

    documents_service.revise(db, sent, actor="staff:aaron")
    after = admin_client.get(f"/admin/bookings/{booking.id}")

    assert "no working link until you send this" in after.text


def test_the_notice_goes_away_once_it_is_sent(admin_client, db, loft):
    booking = _booking(db, loft, "Revise Window Gone")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    draft = documents_service.revise(db, sent, actor="staff:aaron")
    documents_service.mark_sent(db, draft, actor="staff:aaron")

    page = admin_client.get(f"/admin/bookings/{booking.id}")

    assert "no working link" not in page.text


def test_a_first_draft_never_sent_is_not_called_a_revision(admin_client, db, loft):
    """Nothing was ever sent, so no client is holding anything."""
    booking = _booking(db, loft, "Revise First Draft")
    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )

    page = admin_client.get(f"/admin/bookings/{booking.id}")

    assert "no working link" not in page.text


def test_the_client_is_told_the_document_is_being_updated(client, db, loft):
    """Not "this link is no longer active", which is true and useless: they
    do not need to ring anybody, they need to wait."""
    booking = _booking(db, loft, "Revise Client Link")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    token = sent.access_token
    documents_service.revise(db, sent, actor="staff:aaron")

    page = client.get(f"/d/{token}")

    assert page.status_code == 410
    assert "being updated" in page.text
    assert "check back shortly" in page.text.lower()


def test_a_superseded_link_still_says_it_is_dead_once_the_new_one_is_out(client, db, loft):
    """The other case: they were given a newer link, so "no longer active"
    is the honest answer and "check back shortly" would be a lie."""
    booking = _booking(db, loft, "Revise Client Dead")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    token = sent.access_token
    draft = documents_service.revise(db, sent, actor="staff:aaron")
    documents_service.mark_sent(db, draft, actor="staff:aaron")

    page = client.get(f"/d/{token}")

    assert page.status_code == 410
    assert "no longer active" in page.text
    assert "being updated" not in page.text


# --- Triage -------------------------------------------------------------------


def test_triage_does_not_claim_an_issued_event_order_was_never_issued(db, loft):
    """A revise leaves a draft current while the sent version stays sent.
    Gating on is_current made Triage say "no Event Order has been issued"
    about a booking whose client had one in their inbox."""
    from app.models.booking import BookingStatus
    from app.services import reconciliation
    from app.services.booking import change_status

    booking = _booking(db, loft, "Revise Triage")
    booking.event_date = dt.date.today() + dt.timedelta(days=5)
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    sent = _sent_beo(db, booking, room_layout_notes=TYPED)
    documents_service.revise(db, sent, actor="staff:aaron")
    db.flush()
    db.refresh(booking)

    findings = reconciliation.check_imminent_without_beo([booking], today=dt.date.today())

    assert [f.code for f in findings] == [], [f.detail for f in findings]


@pytest.fixture()
def client(db, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
