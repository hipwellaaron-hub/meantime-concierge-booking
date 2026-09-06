"""Event Order proposals: the AI transcribes, Aaron approves.

The feature exists because of one real failure: a client's decoration note
was transcribed by hand into the Dietaries field and a declared nut
allergy was dropped. So the tests that matter most here are the ones that
prove a proposal cannot reach the document on its own, and that the four
house rules block the shapes that error takes.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AiRequestLog, BookingEvent, Contact
from app.models.beo_proposal import (
    FIELD_APPROVED,
    FIELD_PENDING,
    FIELD_REJECTED,
    FIELD_SUPERSEDED,
    STATUS_PENDING,
    STATUS_RESOLVED,
    STATUS_RULES_BLOCKED,
    STATUS_SUPERSEDED,
    BeoProposal,
)
from app.models.document import DocumentStatus, DocumentType
from app.services import beo_proposals, beo_rules
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

TOKEN = "test-ai-token-do-not-use-in-production"

# A clean run-sheet note, in the voice the Event Order is written in.
CLEAN = {
    "catering_order_and_service_style": "Grazing on arrival, mains to share from 7pm.",
    "dietaries": "1x severe nut allergy (table 4). 2x vegetarian.",
}


def _booking(db, space, *, name="Proposal Test", event_type="birthday", adults=40, children=0):
    contact = Contact(name="Proposal Client", email=f"prop.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type=event_type, adult_count=adults, child_count=children, notes=None, actor="test",
    )


def _beo(db, booking, **content_overrides):
    content = generate_beo_content(booking)
    content.update(content_overrides)
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")


@pytest.fixture()
def ai_client(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    monkeypatch.setattr(settings, "ai_writes_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client
    finally:
        app.dependency_overrides.clear()


# --- the four house rules ------------------------------------------------------


@pytest.mark.parametrize("dietaries", [
    "Balloon arch behind the cake table, no nuts",
    "2x GF. Florist delivering at 4pm.",
    "Decorations dropped off Friday",
    "Nut allergy. Set up from 3pm please.",
    "1x vegan. Supplier bringing the backdrop.",
    "No dairy — photographer needs a spot near the bar",
])
def test_decoration_supplier_and_setup_language_is_blocked_from_dietaries(dietaries):
    """The error that prompted the feature: a decoration note transcribed
    into Dietaries, taking a declared allergy down with it."""
    result = beo_rules.validate({"dietaries": dietaries})
    assert beo_rules.DIETARY_CONTAMINATION in result.codes
    assert result.blocked


@pytest.mark.parametrize("dietaries", [
    "1x severe nut allergy (table 4). 2x vegetarian.",
    "3x gluten free, 1x dairy free.",
    "Coeliac — kitchen is 100% GF, confirmed with the client.",
    "Gluten-free cake supplied by the client, stays on the cake table.",
])
def test_a_real_dietary_note_passes(dietaries):
    assert not beo_rules.validate({"dietaries": dietaries}).blocked


@pytest.mark.parametrize("text", [
    "We have organised a DJ for the night",
    "I imagine we'll want the room set out in rounds",
    "Our photographer arrives at 6",
    "I'll bring the cake myself",
    "we're hoping for the Loft",
])
def test_first_person_client_prose_is_blocked(text):
    result = beo_rules.validate({"special_notes": text})
    assert beo_rules.CLIENT_PROSE in result.codes


@pytest.mark.parametrize("text", [
    "DJ from 8pm, client's own playlist before that.",
    "Rounds of 8, dance floor centre.",
    "Client supplying the cake; stays on the cake table.",
    "Onsite contact: Sam, 0400 000 000.",
])
def test_run_sheet_notes_pass(text):
    assert not beo_rules.validate({"special_notes": text}).blocked


def test_the_default_playlist_line_alongside_a_dj_is_blocked():
    music = (
        "Client's own Spotify playlist — set to public, playlist name given to the team on the night "
        "(no links).\nDJ."
    )
    assert beo_rules.MUSIC_CONFLICT in beo_rules.validate({"music": music}).codes


def test_a_playlist_alone_and_a_dj_alone_both_pass():
    playlist = "Client's own Spotify playlist — set to public, playlist name given to the team (no links)."
    assert not beo_rules.validate({"music": playlist}).blocked
    assert not beo_rules.validate({"music": "DJ from 8pm, bump-in 6:30pm."}).blocked


def test_an_18th_with_children_needs_the_rsa_line_in_special_notes():
    without = beo_rules.validate(
        {"special_notes": "Rounds of 8. Cake at 9."},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert beo_rules.RSA_MISSING in without.codes

    with_line = beo_rules.validate(
        {"special_notes": "Rounds of 8. Strict RSA applies; no service to under-18s."},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert not with_line.blocked


def test_the_rsa_floor_reads_the_document_not_just_the_proposal():
    """A proposal that leaves Special notes alone still has to leave the
    finished Event Order carrying the line."""
    blocked = beo_rules.validate(
        {"music": "DJ from 8pm."},
        effective={"music": "DJ from 8pm.", "special_notes": "Rounds of 8."},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert beo_rules.RSA_MISSING in blocked.codes

    already_there = beo_rules.validate(
        {"music": "DJ from 8pm."},
        effective={"music": "DJ from 8pm.", "special_notes": "RSA conditions apply."},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert not already_there.blocked


def test_the_rsa_floor_does_not_fire_without_children_or_without_an_18th():
    assert not beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="18th Birthday", event_name="Milly's 18th", child_count=0
    ).blocked
    assert not beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="birthday", event_name="Kim's 40th", child_count=12
    ).blocked


def test_an_unknown_field_an_empty_proposal_and_an_essay_are_all_refused():
    assert beo_rules.UNKNOWN_FIELD in beo_rules.validate({"total_food_spend": "500"}).codes
    assert beo_rules.EMPTY_PROPOSAL in beo_rules.validate({}).codes
    assert beo_rules.FIELD_TOO_LONG in beo_rules.validate({"special_notes": "x" * 2001}).codes


def test_validation_fails_closed(monkeypatch):
    monkeypatch.setattr(beo_rules, "_validate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = beo_rules.validate({"dietaries": "1x GF"})
    assert result.blocked and beo_rules.RULES_ERROR in result.codes


# --- the endpoint proposes and applies nothing ----------------------------------


def _propose(client, booking, fields, source="client email 6 Sep, final details"):
    return client.post(
        f"/api/ai/bookings/{booking.reference_code}/event-order-proposal",
        json={"source": source, "fields": fields, "trigger": "staff_request", "model": "claude-test"},
    )


def test_a_proposal_is_stored_pending_and_the_document_is_untouched(ai_client, db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking, dietaries="No dietary requirements declared")
    before = dict(document.content)

    resp = _propose(ai_client, booking, CLEAN)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == STATUS_PENDING
    assert sorted(body["awaiting_approval"]) == sorted(CLEAN)
    db.refresh(document)
    assert document.content == before, "a proposal must never write to the Event Order"
    proposal = db.query(BeoProposal).filter_by(booking_id=booking.id).one()
    assert proposal.created_by == "ai:claude"
    assert proposal.source == "client email 6 Sep, final details"
    assert {f.state for f in proposal.fields} == {FIELD_PENDING}


def test_the_endpoint_refuses_the_food_order_the_totals_and_the_status(ai_client, db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    for field in ("food_order", "total_food_spend", "status_text", "line_items"):
        resp = _propose(ai_client, booking, {field: "anything"})
        assert resp.status_code == 422, field
        assert field in resp.json()["detail"]["unknown_fields"]
    assert db.query(BeoProposal).count() == 0, "a refused field must not store a proposal"


def test_a_rule_blocked_proposal_is_stored_but_never_reviewable(ai_client, db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)

    resp = _propose(ai_client, booking, {"dietaries": "Balloon arch, and no nuts"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert beo_rules.DIETARY_CONTAMINATION in detail["rule_codes"]
    proposal = db.query(BeoProposal).filter_by(booking_id=booking.id).one()
    assert proposal.status == STATUS_RULES_BLOCKED
    assert proposal.is_reviewable is False
    assert beo_proposals.review_rows(db, booking.id) == []


def test_the_write_is_logged_as_a_write_against_the_booking(ai_client, db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    _propose(ai_client, booking, CLEAN)
    # require_ai logs every request as a read; the endpoint adds the write
    # row that the write budget actually counts.
    writes = db.query(AiRequestLog).filter_by(kind="write").all()
    assert len(writes) == 1
    assert writes[0].booking_id == booking.id
    assert writes[0].actor == "ai:claude"
    assert writes[0].endpoint.endswith("/event-order-proposal")


def test_the_endpoint_is_behind_the_credential_and_the_write_switch(db, loft, hamilton, monkeypatch):
    booking = _booking(db, loft)
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        url = f"/api/ai/bookings/{booking.reference_code}/event-order-proposal"
        body = {"source": "email", "fields": CLEAN}
        assert client.post(url, json=body).status_code == 401
        monkeypatch.setattr(settings, "ai_writes_enabled", False)
        assert client.post(url, json=body, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_a_booking_at_another_venue_or_no_booking_is_404(ai_client, db, loft):
    resp = ai_client.post(
        "/api/ai/bookings/HAM-99999999-NOPE/event-order-proposal",
        json={"source": "email", "fields": CLEAN},
    )
    assert resp.status_code == 404


# --- approval is the only way anything lands ------------------------------------


def test_approving_one_field_writes_only_that_field(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    before_catering = document.content["catering_order_and_service_style"]
    proposal, result = beo_proposals.propose(
        db, booking, fields=CLEAN, source="client email", actor="ai:claude"
    )
    assert not result.blocked

    dietaries_row = next(f for f in proposal.fields if f.field == "dietaries")
    beo_proposals.approve_field(db, dietaries_row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["dietaries"] == CLEAN["dietaries"]
    assert document.content["catering_order_and_service_style"] == before_catering
    db.refresh(dietaries_row)
    assert dietaries_row.state == FIELD_APPROVED
    assert dietaries_row.applied_value == CLEAN["dietaries"]
    assert dietaries_row.edited_before_approval is False
    assert proposal.status == STATUS_PENDING  # the other field is still waiting


def test_approving_an_edited_value_records_both(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x nut allergy"}, source="client email", actor="ai:claude"
    )
    row = proposal.fields[0]

    beo_proposals.approve_field(
        db, row, actor="staff:aaron@meantime.com.au", value="1x SEVERE nut allergy (table 4)"
    )

    db.refresh(document)
    assert document.content["dietaries"] == "1x SEVERE nut allergy (table 4)"
    db.refresh(row)
    assert row.proposed_value == "1x nut allergy"
    assert row.applied_value == "1x SEVERE nut allergy (table 4)"
    assert row.edited_before_approval is True
    events = [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id)]
    assert any(e.event_type == "beo_proposal_edited" for e in events)


def test_the_audit_trail_names_the_ai_and_the_approver(db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="client email 6 Sep", actor="ai:claude"
    )
    beo_proposals.approve_field(db, proposal.fields[0], actor="staff:aaron@meantime.com.au")

    events = {e.event_type: e for e in db.query(BookingEvent).filter_by(booking_id=booking.id)}
    assert events["beo_proposal_created"].actor == "ai:claude"
    assert events["beo_proposal_created"].old_value == "client email 6 Sep"
    approved = events["beo_proposal_approved"]
    assert approved.actor == "staff:aaron@meantime.com.au"
    assert approved.field_name == "dietaries"
    assert approved.new_value == "1x GF"


def test_rejecting_leaves_the_document_alone(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    before = dict(document.content)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )

    beo_proposals.reject_field(db, proposal.fields[0], actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content == before
    db.refresh(proposal)
    assert proposal.fields[0].state == FIELD_REJECTED
    assert proposal.status == STATUS_RESOLVED


def test_approve_all_writes_every_pending_field_at_once(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(db, booking, fields=CLEAN, source="email", actor="ai:claude")

    beo_proposals.approve_all(db, proposal, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["dietaries"] == CLEAN["dietaries"]
    assert document.content["catering_order_and_service_style"] == CLEAN["catering_order_and_service_style"]
    db.refresh(proposal)
    assert proposal.status == STATUS_RESOLVED
    assert {f.state for f in proposal.fields} == {FIELD_APPROVED}


def test_a_field_cannot_be_approved_twice(db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )
    row = proposal.fields[0]
    beo_proposals.approve_field(db, row, actor="staff:test")
    with pytest.raises(beo_proposals.ProposalError):
        beo_proposals.approve_field(db, row, actor="staff:test")


def test_an_event_order_that_has_been_sent_cannot_be_touched(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )
    before = dict(document.content)
    document.status = DocumentStatus.sent
    db.commit()

    with pytest.raises(beo_proposals.ProposalError):
        beo_proposals.approve_field(db, proposal.fields[0], actor="staff:test")
    with pytest.raises(beo_proposals.ProposalError):
        beo_proposals.approve_all(db, proposal, actor="staff:test")

    db.refresh(document)
    assert document.content == before
    assert proposal.fields[0].state == FIELD_PENDING


def test_with_no_event_order_draft_there_is_nothing_to_approve_onto(db, loft):
    booking = _booking(db, loft)  # no BEO generated
    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )
    assert not result.blocked
    with pytest.raises(beo_proposals.ProposalError):
        beo_proposals.approve_field(db, proposal.fields[0], actor="staff:test")


def test_a_new_proposal_supersedes_what_was_still_pending(db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    first, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF", "music": "DJ from 8pm."}, source="email one", actor="ai:claude"
    )
    beo_proposals.approve_field(db, next(f for f in first.fields if f.field == "music"), actor="staff:test")

    second, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF, 1x DF"}, source="email two", actor="ai:claude"
    )

    db.refresh(first)
    assert first.status == STATUS_SUPERSEDED
    states = {f.field: f.state for f in first.fields}
    assert states["dietaries"] == FIELD_SUPERSEDED
    assert states["music"] == FIELD_APPROVED, "an approval already made is history, not a pending ask"
    assert beo_proposals.pending_proposal(db, booking.id).id == second.id


def test_the_review_rows_show_the_value_each_would_replace(db, loft):
    booking = _booking(db, loft)
    _beo(db, booking, dietaries="1x GF")
    beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF, 1x nut allergy", "music": "DJ from 8pm."},
        source="email", actor="ai:claude",
    )
    rows = {r["field"]: r for r in beo_proposals.review_rows(db, booking.id)}
    assert rows["dietaries"]["current"] == "1x GF"
    assert rows["dietaries"]["replaces_text"] is True
    # Music currently carries generation's own [REVIEW] placeholder. The
    # panel shows exactly what the document says, placeholder included --
    # "current" is a quotation, not a judgement about whether it counts.
    assert rows["music"]["current"].startswith("[REVIEW]")


def test_empty_dietaries_read_as_empty_not_as_the_printed_placeholder(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    assert document.content["dietaries"] == "No dietary requirements declared"
    assert beo_proposals.current_values(document)["dietaries"] == ""


# --- the form ---------------------------------------------------------------------


def test_the_event_order_form_shows_proposed_against_current(admin_client, db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking, dietaries="1x GF")
    beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF, 1x severe nut allergy"}, source="client email 6 Sep",
        actor="ai:claude",
    )

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text

    assert "Proposed by Claude" in page
    assert "client email 6 Sep" in page
    assert "1x GF, 1x severe nut allergy" in page
    assert "Replaces what is on the Event Order now" in page
    assert "Approve all 1" in page


def test_the_panel_is_absent_when_nothing_is_pending(admin_client, db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    assert "Proposed by Claude" not in page


def test_approving_through_the_form_writes_the_field(admin_client, db, loft):
    import re

    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy"}, source="email", actor="ai:claude"
    )
    row = proposal.fields[0]
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={
            "csrf_token": csrf,
            "action": f"approve:{row.id}",
            "value_dietaries": "1x severe nut allergy (table 4)",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(document)
    assert document.content["dietaries"] == "1x severe nut allergy (table 4)"
    db.refresh(row)
    assert row.edited_before_approval is True
    assert row.decided_by.startswith("staff:")


def test_the_booking_page_says_a_proposal_is_waiting(admin_client, db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    page = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "Event Order proposal waiting" in page


def test_every_audit_event_type_fits_the_column(db, loft):
    """BookingEvent.event_type is varchar(30): an event this layer writes
    that overflows it takes the approval down with it."""
    from app.models.booking_event import BookingEvent as BE

    limit = BE.__table__.c.event_type.type.length
    for name in (
        "beo_proposal_created", "beo_proposal_blocked", "beo_proposal_approved",
        "beo_proposal_rejected", "beo_proposal_edited",
    ):
        assert len(name) <= limit, name
