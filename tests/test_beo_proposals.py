"""Event Order proposals: the AI transcribes, Aaron approves.

The feature exists because of one real failure: a client's decoration note
was transcribed by hand into the Dietaries field and a declared nut
allergy was dropped. So the tests that matter most here are the ones that
prove a proposal cannot reach the document on its own, and that the four
house rules block the shapes that error takes.
"""

import datetime as dt
import pathlib
import re
import threading
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AiRequestLog, BookingEvent, Contact
from app.models.beo_proposal import (
    FIELD_APPROVED,
    FIELD_BLOCKED,
    BeoProposalField,
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
from app.services.document_generation import generate_beo_content, rebuild_terms_text

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


def _confirmed(db, booking):
    """A proposal creates the first draft only on a tentative, confirmed or
    completed booking; the tests that expect a draft say so."""
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    change_status(db, booking, BookingStatus.confirmed, actor="test")
    return booking


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


def test_a_playlist_alongside_a_dj_is_a_real_booking_and_passes():
    """The wizard's music step is a multi-select and composes both lines
    when the client picks both, so this is a correct Event Order, not a
    half-finished transcription. A rule used to block it (Aaron's ruling,
    2026-09-06: align to the wizard)."""
    music = (
        "Client's own Spotify playlist — set to public, playlist name given to the team on the night "
        "(no links).\nDJ."
    )
    assert not beo_rules.validate({"music": music}).blocked


def test_the_wizards_own_music_text_never_blocks():
    """Built the way a real submitted wizard builds it, from the wizard's
    own line table -- whatever the client selects, the Event Order that
    results must be transcribable."""
    from itertools import combinations

    from app.services import wizard_generation

    types = list(wizard_generation.MUSIC_TYPE_LINES)
    for size in range(1, len(types) + 1):
        for combo in combinations(types, size):
            text = wizard_generation.build_music_text({"music_types": list(combo)})
            result = beo_rules.validate({"music": text})
            assert not result.blocked, (combo, result.codes)


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


def test_an_unrelated_proposal_on_an_18th_warns_rather_than_withholding_it():
    """A proposal that does not touch Special notes must not be refused
    over the document's own RSA gap -- withholding an allergy transcription
    for an unrelated policy floor is worse than surfacing both (2026-09-06
    review). The warning is shown to the reviewer; the proposal stands."""
    result = beo_rules.validate(
        {"dietaries": "1x severe nut allergy"},
        current={"special_notes": "Rounds of 8.", "dietaries": ""},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert not result.blocked
    assert beo_rules.RSA_ABSENT_ON_DOCUMENT in result.warning_codes

    already_there = beo_rules.validate(
        {"dietaries": "1x severe nut allergy"},
        current={"special_notes": "RSA conditions apply.", "dietaries": ""},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=12,
    )
    assert not already_there.blocked and already_there.warning_codes == []


def test_the_rsa_floor_fires_on_either_trigger_alone():
    """REVERSED on 2026-09-09, and it was a licence matter, not a test tidy.

    This asserted the floor needed an 18th AND children, and that AND is
    what let a guest count remove a compliance line: child_count is 0 both
    when there are no minors and when nobody was asked for the split, so an
    18th booked without that split never required the RSA line at all.

    Aaron: "A client who under-reports or leaves it blank shouldn't be able
    to remove a compliance line from a document. The count should widen
    when the RSA line is needed, never narrow it."

    So each trigger now stands alone.
    """
    # An 18th with NO children recorded -- the case that was silently exempt.
    assert beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="18th Birthday", event_name="Milly's 18th", child_count=0
    ).blocked
    # Under-18s on a booking nobody would call an 18th. RSA applies to them
    # whatever the event is called.
    assert beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="birthday", event_name="Kim's 40th", child_count=12
    ).blocked


def test_the_rsa_floor_still_does_not_fire_when_neither_trigger_is_present():
    """The widening is two reasons, not "always". An adults-only event that
    is not an 18th still has no RSA floor -- otherwise the rule becomes
    furniture on every document."""
    assert not beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="birthday", event_name="Kim's 40th", child_count=0
    ).blocked


def test_the_rsa_message_names_the_reason_it_actually_fired():
    """Two triggers, so "an 18th with children on the booking" is no longer
    true of either. A staff member reading a block has to know which fact
    to check."""
    eighteenth = beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="18th Birthday", event_name="Milly's 18th", child_count=0
    )
    minors = beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="birthday", event_name="Kim's 40th", child_count=12
    )

    assert "This is an 18th" in eighteenth.violations[0].message
    assert "under-18s on this booking" in minors.violations[0].message


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
        detail = resp.json()["detail"]
        assert beo_rules.UNKNOWN_FIELD in detail["rule_codes"]
        assert field in detail["note"] or field in str(detail["violations"])
        assert "dietaries" in detail["proposable_fields"]


def test_a_mixed_payload_is_still_recorded_for_calibration(ai_client, db, loft):
    """A good transcription alongside a field that does not exist: the
    attempt is stored, so what the model was reaching for is readable
    rather than lost (2026-09-06 review)."""
    booking = _booking(db, loft)
    _beo(db, booking)
    resp = _propose(ai_client, booking, {"dietaries": "1x severe nut allergy", "food_order": "[...]"})
    assert resp.status_code == 422
    proposal = db.query(BeoProposal).filter_by(booking_id=booking.id).one()
    assert proposal.status == STATUS_RULES_BLOCKED
    assert [f.field for f in proposal.fields] == ["dietaries"]
    assert proposal.fields[0].state == FIELD_BLOCKED


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
    assert {f.state for f in proposal.fields} == {FIELD_BLOCKED}
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


def test_a_proposal_with_no_event_order_creates_the_first_draft(db, loft):
    """REVERSED 2026-09-11. It used to refuse at approval ("generate one
    first"); Aaron: "The proposal should be able to create the Event Order
    draft if none exists, rather than refusing until I've generated one
    by hand." The draft is what the staff Generate click would build."""
    booking = _confirmed(db, _booking(db, loft))
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None

    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )

    assert not result.blocked
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert draft is not None and draft.version == 1 and draft.status == DocumentStatus.draft
    assert proposal.document_id == draft.id, "the proposal is attached to the draft it made"
    assert draft.content["dietaries"] == "No dietary requirements declared", "created, not applied"
    kinds = [(e.event_type, e.actor) for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all()]
    assert ("document_created", "ai:claude") in kinds
    assert ("beo_draft_by_proposal", "ai:claude") in kinds
    assert ("beo_proposal_created", "ai:claude") in kinds

    approved = beo_proposals.approve_field(db, proposal.fields[0], actor="staff:test")

    assert approved.id == draft.id and approved.content["dietaries"] == "1x GF"


def test_a_blocked_proposal_creates_no_draft(db, loft):
    """A blocked proposal is never offered for review, so there is nothing
    a draft would be for."""
    booking = _confirmed(db, _booking(db, loft))  # a booking that WOULD get a draft
    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "balloons and streamers on every table"}, source="email", actor="ai:claude"
    )

    assert result.blocked, result.codes
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None
    assert proposal.document_id is None


def test_a_sent_event_order_is_never_superseded_by_a_proposal(db, loft):
    """Only when there is NO Event Order. A version that has gone out is
    a Revise, which stays a staff decision."""
    booking = _booking(db, loft)
    sent = documents_service.mark_sent(db, _beo(db, booking), actor="staff:test")

    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )

    assert not result.blocked
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.id == sent.id and current.version == 1 and current.status == DocumentStatus.sent
    assert proposal.document_id is None
    with pytest.raises(beo_proposals.ProposalError):
        beo_proposals.approve_field(db, proposal.fields[0], actor="staff:test")


def test_the_created_draft_is_what_generate_would_build(db, loft, menu_items):
    """One builder for the staff click and the proposal path. With a
    submitted wizard that means the client's own food answers, not blank
    placeholders."""
    from tests.test_wizard_generation import _complete_all_steps, _make_booking, _pay_deposit
    from app.models.wizard_session import WizardSessionStatus
    from app.services import wizard as wizard_service

    booking = _confirmed(db, _make_booking(db, loft))
    _pay_deposit(db, booking)
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")
    _complete_all_steps(db, session, menu_items)
    # The wizard is submitted but its documents never got built -- the
    # state wizard.submit_review leaves behind when generation raises
    # (GenerationFailed): status submitted, no Event Order. Set directly;
    # fresh_beo_content reads only the status and the step answers.
    session.status = WizardSessionStatus.submitted
    db.commit()
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None

    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude"
    )

    draft = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert draft is not None and not result.blocked
    lines = draft.content["food_order"]["line_items"]
    assert [ln["description"] for ln in lines] == ["Grazing Platter"], "the wizard's food, not a placeholder"
    assert draft.content["food_order"]["note"] is None


def test_a_second_proposal_reuses_the_draft_the_first_one_made(db, loft):
    booking = _confirmed(db, _booking(db, loft))
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    first = documents_service.get_current(db, booking.id, DocumentType.beo)

    proposal, _ = beo_proposals.propose(db, booking, fields={"music": "DJ from 8pm"}, source="email 2", actor="ai:claude")

    again = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert again.id == first.id and again.version == 1, "no second version"
    assert proposal.document_id == first.id


def test_the_endpoint_says_it_created_the_draft(ai_client, db, loft):
    booking = _confirmed(db, _booking(db, loft))

    first = _propose(ai_client, booking, CLEAN)
    second = _propose(ai_client, booking, {"music": "DJ from 8pm"})

    assert first.status_code == 201, first.text
    assert first.json()["event_order"] == {"version": 1, "status": "draft", "created": True}
    assert "created the first draft" in first.json()["note"]
    assert second.status_code == 201
    assert second.json()["event_order"] == {"version": 1, "status": "draft", "created": False}


def test_the_booking_page_links_to_the_draft_the_proposal_made(admin_client, db, loft):
    booking = _confirmed(db, _booking(db, loft))
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)

    page = admin_client.get(f"/admin/bookings/{booking.id}").text

    assert "Event Order proposal waiting" in page
    assert f"/documents/{draft.id}/edit" in page, "the review link points at the draft"
    assert "Generate the Event Order to review them" not in page


def test_approve_all_is_the_first_thing_on_the_panel(admin_client, db, loft):
    """Aaron, 2026-09-11: ten per-field clicks when one email produced
    all ten fields. Approve all existed but sat at the bottom under ten
    primary Approve buttons; it is now the first control, and the
    per-field buttons read as the exception."""
    booking = _confirmed(db, _booking(db, loft))
    beo_proposals.propose(db, booking, fields=CLEAN, source="email", actor="ai:claude")
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{draft.id}/edit").text

    panel = page[page.index("Proposed by Claude"):]
    assert panel.index('value="approve_all"') < panel.index('value="approve:'), "Approve all comes first"
    assert "Approve only Dietaries" in panel
    assert panel.count('value="approve_all"') == 2, "and it is still there at the foot of the list"


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
    # Music currently carries generation's own [REVIEW] placeholder: shown
    # verbatim, but not flagged as overwriting content a human wrote.
    assert rows["music"]["current"].startswith("[REVIEW]")
    assert rows["music"]["replaces_text"] is False


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
    assert "Replaces text already on the Event Order" in page
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


def test_every_audit_event_type_fits_the_column():
    """BookingEvent.event_type is varchar(30): an event this layer writes
    that overflows it takes the approval down with it. Read out of the
    source rather than retyped, so renaming one in production is caught."""
    import re

    from app.models.booking_event import BookingEvent as BE

    source = (pathlib.Path(beo_proposals.__file__)).read_text(encoding="utf-8")
    names = set(re.findall(r'event_type="([a-z_]+)"', source))
    assert names, "no event types found -- the pattern stopped matching"
    limit = BE.__table__.c.event_type.type.length
    for name in names:
        assert len(name) <= limit, f"{name} is {len(name)} chars, limit {limit}"


# --- 2026-09-06 review: what the five angles found ------------------------------


def test_a_blank_proposal_cannot_erase_what_the_event_order_says(db, loft):
    """The other half of the incident. A transcription that finds nothing
    must not be able to replace a declared allergy with silence."""
    result = beo_rules.validate({"dietaries": ""}, current={"dietaries": "1x severe nut allergy"})
    assert beo_rules.ERASES_VALUE in result.codes
    # Blank against blank is not an erasure.
    assert not beo_rules.validate({"dietaries": ""}, current={"dietaries": ""}).blocked


def test_a_declared_dietary_can_never_quietly_disappear(db, loft):
    result = beo_rules.validate(
        {"dietaries": "2x vegetarian"}, current={"dietaries": "2x vegetarian, 1x severe nut allergy"}
    )
    assert beo_rules.DROPS_DIETARY in result.codes
    assert "nut" in result.as_note()
    # Adding to it is fine.
    assert not beo_rules.validate(
        {"dietaries": "2x vegetarian, 1x nut allergy, 1x coeliac"},
        current={"dietaries": "2x vegetarian, 1x nut allergy"},
    ).blocked


def test_the_endpoint_refuses_a_proposal_that_would_erase_a_dietary(ai_client, db, loft):
    booking = _booking(db, loft)
    _beo(db, booking, dietaries="1x severe nut allergy")
    resp = _propose(ai_client, booking, {"dietaries": ""})
    assert resp.status_code == 422
    assert beo_rules.ERASES_VALUE in resp.json()["detail"]["rule_codes"]


def test_approving_with_no_textarea_keeps_the_proposed_value(admin_client, db, loft):
    """Proven live by the review: with the boxes defaulting to "", a POST
    carrying only the action blanked a declared allergy and blamed the
    staff member for editing it."""
    import re

    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)"}, source="email", actor="ai:claude"
    )
    row = proposal.fields[0]
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={"csrf_token": csrf, "action": f"approve:{row.id}"},  # no value_* at all
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(document)
    assert document.content["dietaries"] == "1x severe nut allergy (table 4)"
    db.refresh(row)
    assert row.applied_value == "1x severe nut allergy (table 4)"
    assert row.edited_before_approval is False


def test_approve_all_with_no_textareas_blanks_nothing(admin_client, db, loft):
    import re

    booking = _booking(db, loft)
    document = _beo(db, booking)
    proposal, _ = beo_proposals.propose(db, booking, fields=CLEAN, source="email", actor="ai:claude")
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={"csrf_token": csrf, "action": "approve_all"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db.refresh(document)
    assert document.content["dietaries"] == CLEAN["dietaries"]
    assert document.content["catering_order_and_service_style"] == CLEAN["catering_order_and_service_style"]


def test_the_rules_run_again_on_what_the_box_says_at_approval(admin_client, db, loft):
    """The box is editable, so a propose-time-only gate could be walked
    past by pasting the original incident straight into it."""
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
            "csrf_token": csrf, "action": f"approve:{row.id}",
            "value_dietaries": "Balloon arch behind the cake table",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 409
    db.refresh(document)
    assert document.content["dietaries"] == "No dietary requirements declared"
    db.refresh(row)
    assert row.state == FIELD_PENDING, "a refused approval leaves the field to be decided again"


def test_a_browser_rewriting_newlines_is_not_a_human_edit(db, loft):
    """Textareas submit CRLF. Recording that as an edit would corrupt the
    one number this feature exists to produce."""
    booking = _booking(db, loft)
    document = _beo(db, booking)
    multiline = "Grazing on arrival\nAlternate drop from 7pm"
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"catering_order_and_service_style": multiline}, source="email", actor="ai:claude"
    )
    row = proposal.fields[0]

    beo_proposals.approve_field(db, row, actor="staff:test", value=multiline.replace("\n", "\r\n"))

    db.refresh(row)
    assert row.edited_before_approval is False
    db.refresh(document)
    assert "\r" not in document.content["catering_order_and_service_style"]


def test_two_approvals_at_the_same_moment_both_survive():
    """Reproduced by the review before the fix: each approval wrote a whole
    content blob built from a snapshot taken before the row lock, so one
    silently reverted the other while both audits claimed success. Real
    sessions and real commits, so the row locks actually engage."""
    from sqlalchemy import text as sql_text

    from app.models import Contact
    from app.models.document import DocumentType
    from app.seed import seed as seed_hamilton
    from app.services import documents as documents_service
    from app.services.booking import create_booking
    from app.services.document_generation import generate_beo_content
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    space = next(s for s in venue.spaces if s.is_bookable)
    email = f"race.beo.{uuid.uuid4().hex[:8]}@example.com"
    contact = Contact(name="Race BEO", email=email)
    setup.add(contact)
    setup.flush()
    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=f"Race BEO {uuid.uuid4().hex[:6]}",
        event_type="corporate", adult_count=40, child_count=0, notes=None, actor="test",
    )
    documents_service.create_new_version(
        setup, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    proposal, _ = beo_proposals.propose(
        setup, booking, fields=CLEAN, source="race email", actor="ai:claude"
    )
    booking_id = booking.id
    ids = {f.field: f.id for f in proposal.fields}
    setup.close()

    barrier = threading.Barrier(2)
    errors = {}

    def approve(name, field_id):
        session = TestSessionLocal()
        try:
            row = session.get(BeoProposalField, field_id)
            barrier.wait(timeout=5)
            beo_proposals.approve_field(session, row, actor=f"staff:{name}")
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc
        finally:
            session.close()

    threads = [
        threading.Thread(target=approve, args=("a", ids["catering_order_and_service_style"])),
        threading.Thread(target=approve, args=("b", ids["dietaries"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = TestSessionLocal()
    try:
        assert not errors, errors
        document = beo_proposals.current_draft_beo(check, booking_id)
        assert document.content["dietaries"] == CLEAN["dietaries"]
        assert document.content["catering_order_and_service_style"] == CLEAN["catering_order_and_service_style"]
    finally:
        check.execute(sql_text("SET LOCAL app.allow_booking_purge='on'"))
        target = check.get(type(booking), booking_id)
        if target is not None:
            from app.services.booking import delete_booking_and_dependents
            delete_booking_and_dependents(check, target, actor="staff:test")
        check.close()


def test_an_approval_in_flight_is_not_overwritten_by_a_new_proposal():
    """Found by the review of this branch: _supersede_older read the
    pending fields without a lock, so a proposal arriving while Aaron
    was approving a field would block behind his row lock at commit and
    then write SUPERSEDED over APPROVED -- the document kept the value
    while the record said it was never applied. Real sessions: A holds
    the field row lock the way approve_field does, B proposes while A
    holds it, A approves and commits, B must then see the approval."""
    import time

    from sqlalchemy import text as sql_text

    from app.models import Contact
    from app.models.document import DocumentType
    from app.seed import seed as seed_hamilton
    from app.services import documents as documents_service
    from app.services.booking import create_booking
    from app.services.document_generation import generate_beo_content
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    space = next(s for s in venue.spaces if s.is_bookable)
    email = f"race2.beo.{uuid.uuid4().hex[:8]}@example.com"
    contact = Contact(name="Race BEO 2", email=email)
    setup.add(contact)
    setup.flush()
    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 21),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=f"Race BEO 2 {uuid.uuid4().hex[:6]}",
        event_type="corporate", adult_count=40, child_count=0, notes=None, actor="test",
    )
    documents_service.create_new_version(
        setup, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    first, _ = beo_proposals.propose(
        setup, booking, fields=CLEAN, source="race email one", actor="ai:claude"
    )
    booking_id = booking.id
    dietaries_id = next(f.id for f in first.fields if f.field == "dietaries")
    setup.close()

    errors = {}
    session_a = TestSessionLocal()
    row = session_a.get(BeoProposalField, dietaries_id)
    session_a.refresh(row, with_for_update=True)          # A is mid-approval: the row is his

    def propose_second():
        session_b = TestSessionLocal()
        try:
            b_booking = session_b.get(type(booking), booking_id)
            beo_proposals.propose(
                session_b, b_booking, fields={"music": "Acoustic duo from 7pm."},
                source="race email two", actor="ai:claude",
            )
        except Exception as exc:  # noqa: BLE001
            errors["b"] = exc
        finally:
            session_b.close()

    thread = threading.Thread(target=propose_second)
    thread.start()
    time.sleep(0.5)                                          # B is now waiting on A's lock (or, unfixed, has read PENDING)
    try:
        beo_proposals.approve_field(session_a, row, actor="staff:a")
    except Exception as exc:  # noqa: BLE001
        errors["a"] = exc
    finally:
        session_a.close()
    thread.join(timeout=10)

    check = TestSessionLocal()
    try:
        assert not errors, errors
        field = check.get(BeoProposalField, dietaries_id)
        assert field.state == FIELD_APPROVED, "the approval that committed first is the record"
        superseded_dietaries = [
            e for e in check.query(BookingEvent).filter_by(booking_id=booking_id)
            if e.event_type == "beo_proposal_superseded" and e.field_name == "dietaries"
        ]
        assert superseded_dietaries == [], "no audit row may claim an applied field was dropped"
        document = beo_proposals.current_draft_beo(check, booking_id)
        assert document.content["dietaries"] == CLEAN["dietaries"]
        assert beo_proposals.pending_proposal(check, booking_id).source == "race email two"
    finally:
        check.execute(sql_text("SET LOCAL app.allow_booking_purge='on'"))
        target = check.get(type(booking), booking_id)
        if target is not None:
            from app.services.booking import delete_booking_and_dependents
            delete_booking_and_dependents(check, target, actor="staff:test")
        check.close()


def test_only_one_proposal_is_ever_pending(db, loft):
    booking = _booking(db, loft)
    _beo(db, booking)
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="one", actor="ai:claude")
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF, 1x DF"}, source="two", actor="ai:claude")
    pending = db.query(BeoProposal).filter_by(booking_id=booking.id, status=STATUS_PENDING).all()
    assert len(pending) == 1


def test_superseding_a_field_nobody_saw_leaves_an_audit_row(db, loft):
    """A proposed allergy that vanished because a newer ask arrived has to
    be explainable from the booking timeline."""
    booking = _booking(db, loft)
    _beo(db, booking)
    beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy"}, source="one", actor="ai:claude"
    )
    beo_proposals.propose(db, booking, fields={"music": "DJ from 8pm."}, source="two", actor="ai:claude")

    events = [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id)]
    superseded = [e for e in events if e.event_type == "beo_proposal_superseded"]
    assert len(superseded) == 1
    assert superseded[0].field_name == "dietaries"
    assert "nut allergy" in superseded[0].old_value


def test_a_cancelled_booking_and_a_linked_room_refuse_proposals(ai_client, db, loft, mezzanine):
    from app.models.booking import BookingStatus
    from app.services.booking import add_linked_space, change_status

    cancelled = _booking(db, loft, name="Cancelled Party")
    _beo(db, cancelled)
    change_status(db, cancelled, BookingStatus.cancelled, actor="staff:test")
    resp = _propose(ai_client, cancelled, {"dietaries": "1x GF"})
    assert resp.status_code == 409
    assert db.query(BeoProposal).filter_by(booking_id=cancelled.id).count() == 0

    parent = _booking(db, loft, name="Two Room Party")
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="staff:test")
    resp = _propose(ai_client, child, {"dietaries": "1x GF"})
    assert resp.status_code == 409


def test_a_booking_can_still_be_deleted_after_the_ai_has_proposed(ai_client, db, loft):
    """The write log's booking_id had never been populated before this
    endpoint; its foreign key made the booking undeletable (proven by the
    2026-09-06 review)."""
    from app.services.booking import delete_booking_and_dependents

    booking = _booking(db, loft, name="Delete Me")
    _beo(db, booking)
    assert _propose(ai_client, booking, CLEAN).status_code == 201

    reference = delete_booking_and_dependents(db, booking, actor="staff:test")

    assert reference
    assert db.query(BeoProposal).filter_by(booking_id=booking.id).count() == 0
    assert db.query(AiRequestLog).filter_by(booking_id=booking.id).count() == 0


def test_reading_the_proposal_back_does_not_spend_or_trip_the_write_budget(ai_client, db, loft):
    """The GET sat behind the write gate, and enforce_write_budget mutates
    state: one read re-disabled writes the moment staff re-enabled them."""
    from app.services import ai_access

    booking = _booking(db, loft)
    _beo(db, booking)
    _propose(ai_client, booking, CLEAN)
    writes_before = db.query(AiRequestLog).filter_by(kind="write").count()

    resp = ai_client.get(f"/api/ai/bookings/{booking.reference_code}/event-order-proposal")

    assert resp.status_code == 200
    assert db.query(AiRequestLog).filter_by(kind="write").count() == writes_before
    assert ai_access.writes_enabled(db) is True


def test_a_proposal_for_another_venues_booking_is_not_found(ai_client, db, loft):
    """Venue scoping, actually exercised: the reference exists, but not at
    the venue the credential is for."""
    from app.models import Space, Venue

    other = Venue(name="Meantime The Entrance", slug="entrance", reference_prefix="ENT")
    db.add(other)
    db.flush()
    other_space = Space(
        venue_id=other.id, name="The Deck", capacity=60, standard_min_adults=20,
        min_food_spend=Decimal("0"), is_bookable=True,
    )
    db.add(other_space)
    db.flush()
    elsewhere = _booking(db, other_space, name="Entrance Party")

    resp = _propose(ai_client, elsewhere, {"dietaries": "1x GF"})

    assert resp.status_code == 404
    assert db.query(BeoProposal).filter_by(booking_id=elsewhere.id).count() == 0


# --- the widened validators -------------------------------------------------------


@pytest.mark.parametrize("dietaries", [
    "Decor being dropped off at 4pm",
    "Grazing table hire, no nuts",
    "Photo booth 7pm. 1x GF.",
    "Styled by the client",
    "Props arriving Friday",
    "Neon sign behind the bar",
    "Table runners in sage",
    "Chair sashes on the bridal table",
])
def test_the_wider_decoration_vocabulary_is_blocked_from_dietaries(dietaries):
    assert beo_rules.DIETARY_CONTAMINATION in beo_rules.validate({"dietaries": dietaries}).codes


def test_invisible_characters_do_not_smuggle_a_word_past_the_dietary_rule():
    assert beo_rules.DIETARY_CONTAMINATION in beo_rules.validate({"dietaries": "ball\u200doons, no nuts"}).codes


@pytest.mark.parametrize("text", [
    "im bringing the cake",
    "ive organised a DJ",
    "weve booked a photographer",
    "Would like the room in rounds",
    "Hoping for a 6pm start",
    "Please can the cake go out at 9",
])
def test_client_voice_without_a_pronoun_or_an_apostrophe_is_blocked(text):
    assert beo_rules.CLIENT_PROSE in beo_rules.validate({"special_notes": text}).codes


def test_the_rsa_wording_the_venue_actually_uses_is_not_blocked_as_prose():
    """The RSA rule effectively asks for this sentence; the prose rule
    used to refuse it because of the I in I.D."""
    result = beo_rules.validate(
        {"special_notes": "RSA applies. I.D. checks at the door, no service to under 18s."},
        event_type="18th Birthday", event_name="Milly's 18th", child_count=4,
    )
    assert not result.blocked, result.codes


@pytest.mark.parametrize("music, entertainment", [
    ("Client's own Spotify playlist, public, name on the night. DJs from 8pm.", None),
    ("Client's own Spotify playlist, public, name on the night.", "Deejay from 8pm"),
    ("Client's own Spotify playlist, public, name on the night.", None),
    ("DJ from 8pm.", None),
])
def test_no_music_combination_is_blocked(music, entertainment):
    """The removed rule blocked the first two. Kept as a regression guard so
    the rule is not reintroduced: the wizard allows every one of these."""
    proposed = {"music": music}
    if entertainment is not None:
        proposed["entertainment"] = entertainment
    assert not beo_rules.validate(proposed).blocked


def test_a_date_in_the_event_name_is_not_an_18th():
    """The rule delegates to the one definition the rest of the system
    uses, which requires birthday context rather than the digits alone.

    child_count is deliberately 0: since the RSA floor fires on under-18s
    alone as well, any non-zero count here would block for a reason that
    has nothing to do with what this test is about."""
    assert not beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="corporate", event_name="Team lunch 18 Nov", child_count=0
    ).blocked
    assert beo_rules.RSA_MISSING in beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="birthday", event_name="Milly's 18th birthday", child_count=4
    ).codes


# --- the review screen ------------------------------------------------------------


def test_the_waiting_notice_is_not_buried_in_a_collapsed_section(admin_client, db, loft):
    """It was rendered inside the collapsed "Traffic source" card, where
    the test passed and no human would ever have seen it."""
    booking = _booking(db, loft)
    _beo(db, booking)
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")

    page = admin_client.get(f"/admin/bookings/{booking.id}").text

    # The base template's stylesheet mentions <details class="card"> in a
    # comment, so count tags in the body only.
    import re

    body = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    notice = body.index("Event Order proposal waiting")
    before = body[:notice]
    assert before.count("<details") == before.count("</details>"),         "the notice is inside a collapsed <details> -- nobody would see it"
    assert notice < body.index("Traffic source")


def test_the_panel_does_not_render_on_a_superseded_draft(admin_client, db, loft):
    """Two draft versions: the panel belongs only on the one approval
    would actually write to, or the page shows one document's values above
    a form that edits another."""
    booking = _booking(db, loft)
    old = _beo(db, booking)
    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    _beo(db, booking)  # a new current version; `old` stays draft but not current

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{old.id}/edit").text

    assert "Proposed by Claude" not in page


# --- regenerate must not silently destroy a human value ------------------------
#
# Aaron, 2026-09-06: "Silent loss of a declared allergy is the thing all of
# this work exists to prevent." Regenerate was doing exactly that, proved
# live before the fix: one click replaced "1x severe nut allergy (table 4)."
# with the generator's default "No dietary requirements declared" and made
# it version 2, with nothing shown to anyone.


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _regenerate(client, booking_id, csrf, doc_type="beo"):
    return client.post(
        f"/admin/bookings/{booking_id}/documents/{doc_type}/generate", data={"csrf_token": csrf}
    )


def test_regenerate_refuses_to_silently_replace_a_declared_allergy(admin_client, db, loft):
    booking = _booking(db, loft, name="Regenerate Allergy")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).")

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code == 409
    assert "1x severe nut allergy (table 4)." in resp.text
    assert "No dietary requirements declared" in resp.text  # what it would become
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.version == 1, "the version must not have been written"
    assert current.content["dietaries"] == "1x severe nut allergy (table 4)."


def test_the_allergy_survives_when_the_default_choice_is_taken(admin_client, db, loft):
    """Every box arrives ticked, so submitting the form untouched keeps
    everything. The safe answer must require no action."""
    booking = _booking(db, loft, name="Regenerate Keep")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).", room_layout_notes="Rounds of 8.")

    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)
    # The form as rendered: every checkbox is checked.
    keep = re.findall(r'name="keep" value="([^"]+)" checked', shown.text)
    assert set(keep) == {"dietaries", "room_layout_notes"}

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": keep},
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.version == 2, "a new version was still created"
    assert current.content["dietaries"] == "1x severe nut allergy (table 4)."
    assert current.content["room_layout_notes"] == "Rounds of 8."


def test_unticking_a_field_lets_the_regenerated_value_through(admin_client, db, loft):
    booking = _booking(db, loft, name="Regenerate Replace")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).", room_layout_notes="Rounds of 8.")

    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)

    # Keep the allergy, let the layout note be rebuilt.
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": ["dietaries"]},
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["dietaries"] == "1x severe nut allergy (table 4)."
    assert "Rounds of 8." not in (current.content["room_layout_notes"] or "")


def test_the_decision_is_recorded_field_by_field(admin_client, db, loft):
    booking = _booking(db, loft, name="Regenerate Audit")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).", room_layout_notes="Rounds of 8.")
    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": ["dietaries"]},
    )
    db.expire_all()
    event = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="document_regenerated").one()
    assert "kept Dietaries" in event.new_value
    assert "replaced Room layout notes" in event.new_value
    assert event.actor.startswith("staff:")


def test_a_regenerate_with_nothing_to_lose_is_still_one_click(admin_client, db, loft):
    """The ordinary case must not have grown a confirmation step."""
    booking = _booking(db, loft, name="Regenerate Clean")
    _beo(db, booking)  # placeholders only -- nothing a human wrote

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code in (200, 303)
    db.expire_all()
    assert documents_service.get_current(db, booking.id, DocumentType.beo).version == 2


def test_the_choice_is_refused_when_the_values_changed_underneath_it(admin_client, db, loft):
    """Compare-and-set: between the screen and the submit, another approval
    landed. The human answered a question about a value that is no longer
    the one at risk, so the answer must not be applied."""
    booking = _booking(db, loft, name="Regenerate Race")
    doc = _beo(db, booking, dietaries="1x severe nut allergy (table 4).")
    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    stale_expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)

    documents_service.update_content_fields(
        db, doc, {"dietaries": "1x severe nut allergy (table 4). 2x coeliac."}, actor="staff:someone_else"
    )

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": stale_expect, "keep": []},
    )
    assert resp.status_code == 409, "must re-ask, not write"
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.version == 1
    assert "coeliac" in current.content["dietaries"], "the newer value is intact"


def test_the_kept_value_comes_from_the_document_not_the_form(admin_client, db, loft):
    """A value that travelled through a browser and back is not the one that
    was approved. Only the field NAME is taken from the form."""
    booking = _booking(db, loft, name="Regenerate Tamper")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).")
    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={
            "csrf_token": csrf, "expect": expect, "keep": ["dietaries"],
            # A tampered payload naming a value and a field outside the set.
            "dietaries": "No dietary requirements declared",
            "keep_value": "nothing to see",
        },
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["dietaries"] == "1x severe nut allergy (table 4)."


def test_an_approved_value_is_named_as_approved_on_the_screen(admin_client, db, loft, staff_user):
    booking = _booking(db, loft, name="Regenerate Approved")
    doc = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="client email 6 Sep", actor="ai:claude",
    )
    beo_proposals.approve_field(db, proposal.fields[0], actor=f"staff:{staff_user.email}")
    db.expire_all()

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code == 409
    assert "approved by" in resp.text
    assert staff_user.email in resp.text


def test_the_protected_fields_cover_every_proposable_field():
    """The two lists must not drift: a field a proposal can write is a field
    a regenerate must not silently destroy."""
    from app.services import document_regeneration

    assert set(beo_rules.PROPOSABLE_FIELDS) <= set(document_regeneration.PROTECTED_FIELD_NAMES)


def test_a_generated_placeholder_is_not_treated_as_something_to_lose():
    """[REVIEW] prompts and the dietaries default are what the generator
    emits when nothing was captured. Keeping those would be keeping noise --
    and the dietaries default is the very sentence that overwrote a real
    allergy, so it must never count as worth protecting."""
    from app.services import document_regeneration as dr

    assert dr._is_disposable("[REVIEW] add room layout notes")
    assert dr._is_disposable("No dietary requirements declared")
    assert dr._is_disposable("")
    assert not dr._is_disposable("1x severe nut allergy (table 4).")


@pytest.mark.parametrize("value", [
    "Client bringing cake. [REVIEW] confirm nut-free with kitchen",
    "[REVIEW] with Aaron: client wants the nut allergy read back to the kitchen",
    "Rounds of 8. [REVIEW]",
])
def test_a_staff_note_that_mentions_the_review_marker_is_still_protected(value):
    """Staff reuse the [REVIEW] convention in their own notes. A substring
    test classed those as placeholders and regenerated over them without
    asking -- including one carrying an allergy follow-up (2026-09-06
    review). Only an exact generated placeholder is disposable."""
    from app.services import document_regeneration as dr

    assert not dr._is_disposable(value)


def test_every_generated_placeholder_is_recognised(db, loft):
    """Keeps the exact-match set in step with the generator. A bare booking
    produces only placeholders, so every protected field it fills must be
    disposable -- otherwise a first regenerate would ask about fields no
    human has ever touched, and staff would learn to click through."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Placeholder Drift")
    content = generate_beo_content(booking)
    for spec in dr.PROTECTED_FIELDS:
        rendered = spec.render(content.get(spec.name))
        assert dr._is_disposable(rendered), (spec.name, rendered)


# --- the agreement is the contract, and it is protected too --------------------


def _agreement(db, booking, **overrides):
    from app.services.document_generation import generate_agreement_content

    content = generate_agreement_content(booking)
    content.update(overrides)
    return documents_service.create_new_version(
        db, booking, DocumentType.agreement, content, actor="staff:test"
    )


CLAUSE = "Client may bring their own celebrant. Agreed by Aaron."


def test_regenerating_an_agreement_refuses_to_discard_a_hand_edited_clause(admin_client, db, loft):
    """Proved live before the fix: the clause was gone at version 2 with no
    confirmation. terms_sections is a list of dicts, so the text-only
    comparison could not even see it."""
    booking = _booking(db, loft, name="Agreement Clause")
    doc = _agreement(db, booking)
    edited = dict(doc.content)
    edited["terms_sections"] = [{"heading": "Special condition", "body": CLAUSE}]
    documents_service.update_content(db, doc, edited, actor="staff:aaron")
    db.expire_all()

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id), doc_type="agreement")

    assert resp.status_code == 409
    assert "Special condition" in resp.text
    assert CLAUSE in resp.text
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.agreement)
    assert current.version == 1
    assert any(CLAUSE in (s.get("body") or "") for s in current.content["terms_sections"])


def test_keeping_the_agreement_terms_keeps_the_text_rebuilt_from_them(admin_client, db, loft):
    """terms_text is derived from terms_sections. Keeping one and
    regenerating the other would leave the contract stating two different
    sets of terms."""
    booking = _booking(db, loft, name="Agreement Companion")
    doc = _agreement(db, booking)
    edited = dict(doc.content)
    edited["terms_sections"] = [{"heading": "Special condition", "body": CLAUSE}]
    edited["terms_text"] = rebuild_terms_text(edited["terms_sections"])
    documents_service.update_content(db, doc, edited, actor="staff:aaron")
    db.expire_all()

    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf, doc_type="agreement")
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": ["terms_sections"]},
    )
    assert resp.status_code in (200, 303)
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.agreement)
    assert any(CLAUSE in (s.get("body") or "") for s in current.content["terms_sections"])
    assert CLAUSE in current.content["terms_text"], "the derived text travelled with the sections"


def test_an_untouched_agreement_still_regenerates_in_one_click(admin_client, db, loft):
    booking = _booking(db, loft, name="Agreement Clean")
    _agreement(db, booking)

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id), doc_type="agreement")

    assert resp.status_code in (200, 303)
    db.expire_all()
    assert documents_service.get_current(db, booking.id, DocumentType.agreement).version == 2


# --- the decision and the version are one transaction --------------------------


def test_the_audit_note_is_written_with_the_version_not_after_it(admin_client, db, loft):
    """create_new_version commits. Adding the note afterwards meant a crash
    in between left the values discarded with no record of the decision --
    and that record is the measure of whether this feature works."""
    booking = _booking(db, loft, name="Regenerate One Txn")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).", room_layout_notes="Rounds of 8.")
    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": ["dietaries"]},
    )
    db.expire_all()
    regenerated = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="document_regenerated").one()
    assert "kept Dietaries" in regenerated.new_value
    assert "replaced Room layout notes" in regenerated.new_value


def test_the_note_is_written_by_create_new_version_itself(db, loft):
    """The route used to add the note AFTER create_new_version had already
    committed, so a crash in between left the values discarded with no
    record of the decision. The note is now an argument to the call that
    creates the version, which is what makes them one transaction."""
    booking = _booking(db, loft, name="One Transaction")
    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test",
        regenerated_note="kept Dietaries; replaced Room layout notes",
    )
    events = db.query(BookingEvent).filter_by(booking_id=booking.id).all()
    types = {e.event_type for e in events}
    assert "document_created" in types and "document_regenerated" in types
    note = next(e for e in events if e.event_type == "document_regenerated")
    assert note.new_value.startswith("v1: kept Dietaries")


def test_an_ordinary_regenerate_writes_no_regenerated_note(db, loft):
    """The note exists to record a human's keep/replace decision. A
    regenerate with nothing at risk made no decision, so it must not claim
    one."""
    booking = _booking(db, loft, name="No Note")
    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    assert db.query(BookingEvent).filter_by(
        booking_id=booking.id, event_type="document_regenerated"
    ).count() == 0



# --- a regenerate and an approval in flight together ---------------------------
#
# Real sessions and real commits, so the row locks actually engage. Before
# the fix the regenerate read the document without a lock and the approval
# was silently reverted, with the audit line still saying "kept Dietaries"
# (proved live, 2026-09-06 review).


def _race_setup(name):
    from app.models import Contact
    from app.seed import seed as seed_hamilton
    from app.services.booking import create_booking
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    space = next(sp for sp in venue.spaces if sp.is_bookable)
    contact = Contact(name=name, email=f"race.{uuid.uuid4().hex[:8]}@example.com")
    setup.add(contact)
    setup.flush()
    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=f"{name} {uuid.uuid4().hex[:6]}",
        event_type="corporate", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking)
    content["dietaries"] = "1x severe nut allergy (table 4)."
    documents_service.create_new_version(setup, booking, DocumentType.beo, content, actor="staff:test")
    proposal, _ = beo_proposals.propose(
        setup, booking, fields={"dietaries": "1x severe nut allergy (table 4). 2x coeliac."},
        source="second email", actor="ai:claude",
    )
    ids = (booking.id, proposal.fields[0].id, type(booking))
    setup.close()
    return ids


def _purge(booking_type, booking_id):
    from sqlalchemy import text as sql_text

    from app.services.booking import delete_booking_and_dependents
    from tests.conftest import TestSessionLocal

    check = TestSessionLocal()
    try:
        check.execute(sql_text("SET LOCAL app.allow_booking_purge='on'"))
        target = check.get(booking_type, booking_id)
        if target is not None:
            delete_booking_and_dependents(check, target, actor="staff:test")
    finally:
        check.close()


@pytest.mark.usefixtures("hamilton")
def test_an_approval_landing_during_a_regenerate_is_never_silently_reverted():
    """Regenerate holds the lock first. The approval must NOT quietly write
    itself onto the superseded version and report success -- it must fail
    loudly so the human knows their approval did not apply."""
    import time as _time

    from app.services import document_regeneration as dr
    from tests.conftest import TestSessionLocal

    booking_id, field_id, booking_type = _race_setup("Race Regen")
    barrier = threading.Barrier(2)
    errors = {}

    def regenerate():
        session = TestSessionLocal()
        try:
            bk = session.get(booking_type, booking_id)
            fresh = generate_beo_content(bk)
            current = documents_service.lock_current_for_update(session, booking_id, DocumentType.beo)
            found = dr.losses(session, current, fresh)
            barrier.wait(timeout=5)
            _time.sleep(0.4)
            merged = dr.apply_choices(fresh, current, {"dietaries"})
            documents_service.create_new_version(
                session, bk, DocumentType.beo, merged, actor="staff:aaron",
                regenerated_note=dr.summarise(found, {"dietaries"}),
            )
        except Exception as exc:  # noqa: BLE001
            errors["regen"] = exc
        finally:
            session.close()

    def approve():
        session = TestSessionLocal()
        try:
            row = session.get(BeoProposalField, field_id)
            barrier.wait(timeout=5)
            beo_proposals.approve_field(session, row, actor="staff:liz")
        except Exception as exc:  # noqa: BLE001
            errors["approve"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=regenerate), threading.Thread(target=approve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert "regen" not in errors, errors
        # The approval is refused, in words that say what to do next.
        assert isinstance(errors.get("approve"), beo_proposals.ProposalError), errors
        assert "replaced by a newer version" in str(errors["approve"])

        check = TestSessionLocal()
        try:
            document = beo_proposals.current_draft_beo(check, booking_id)
            # The allergy the human chose to keep is on the live version...
            assert document.content["dietaries"] == "1x severe nut allergy (table 4)."
            # ...and the refused approval left no mark anywhere.
            row = check.get(BeoProposalField, field_id)
            assert row.state == FIELD_PENDING
            assert row.applied_value is None
        finally:
            check.close()
    finally:
        _purge(booking_type, booking_id)


@pytest.mark.usefixtures("hamilton")
def test_an_approval_that_lands_first_makes_the_regenerate_re_ask():
    """The other ordering. The approval commits, so the values the human was
    shown are no longer the ones at risk: the fingerprint must not match and
    the decision must not be applied."""
    from app.services import document_regeneration as dr
    from tests.conftest import TestSessionLocal

    booking_id, field_id, booking_type = _race_setup("Race Approve")
    try:
        first = TestSessionLocal()
        try:
            bk = first.get(booking_type, booking_id)
            fresh = generate_beo_content(bk)
            document = documents_service.get_current(first, booking_id, DocumentType.beo)
            shown = dr.losses(first, document, fresh)
            # Both halves of the token, exactly as the route computes it:
            # the approval below changes the losses AND removes the pending
            # row, and either alone must refuse the stale decision.
            expect = dr.fingerprint(shown, beo_proposals.review_rows(first, booking_id, document=document))
        finally:
            first.close()

        approver = TestSessionLocal()
        try:
            beo_proposals.approve_field(approver, approver.get(BeoProposalField, field_id), actor="staff:liz")
        finally:
            approver.close()

        second = TestSessionLocal()
        try:
            bk = second.get(booking_type, booking_id)
            fresh = generate_beo_content(bk)
            current = documents_service.lock_current_for_update(second, booking_id, DocumentType.beo)
            now = dr.losses(second, current, fresh)
            still_pending = beo_proposals.review_rows(second, booking_id, document=current)
            assert dr.fingerprint(now, still_pending) != expect, "the stale decision must be refused"
            assert "coeliac" in current.content["dietaries"]
        finally:
            second.close()
    finally:
        _purge(booking_type, booking_id)


@pytest.mark.usefixtures("hamilton")
def test_an_approval_is_not_destroyed_by_a_regenerate_that_saw_nothing_at_risk():
    """The other half of the same window, found on re-review. When the
    current document holds nothing human, the regenerate takes the
    straight-through branch -- and that branch was reading without a lock
    and then writing on what it read. An approval landing in between was
    destroyed with NO confirmation screen shown and the approver told it
    had succeeded, which is worse than the case the screen was built for.
    """
    import time as _time

    from app.services import document_regeneration as dr
    from tests.conftest import TestSessionLocal

    booking_id, field_id, booking_type = _race_setup("Race Clean")
    # A current Event Order holding only generated placeholders: losses == [].
    setup = TestSessionLocal()
    try:
        bk = setup.get(booking_type, booking_id)
        documents_service.create_new_version(
            setup, bk, DocumentType.beo, generate_beo_content(bk), actor="staff:test"
        )
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    errors = {}

    def regenerate():
        session = TestSessionLocal()
        try:
            bk = session.get(booking_type, booking_id)
            fresh = generate_beo_content(bk)
            current = documents_service.lock_current_for_update(session, booking_id, DocumentType.beo)
            assert not dr.losses(session, current, fresh)
            barrier.wait(timeout=5)
            _time.sleep(0.4)
            documents_service.create_new_version(session, bk, DocumentType.beo, fresh, actor="staff:aaron")
        except Exception as exc:  # noqa: BLE001
            errors["regen"] = exc
        finally:
            session.close()

    def approve():
        session = TestSessionLocal()
        try:
            row = session.get(BeoProposalField, field_id)
            barrier.wait(timeout=5)
            beo_proposals.approve_field(session, row, actor="staff:liz")
        except Exception as exc:  # noqa: BLE001
            errors["approve"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=regenerate), threading.Thread(target=approve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert "regen" not in errors, errors
        # The approval must be refused out loud, never silently dropped.
        assert isinstance(errors.get("approve"), beo_proposals.ProposalError), errors
        assert "replaced by a newer version" in str(errors["approve"])
        check = TestSessionLocal()
        try:
            row = check.get(BeoProposalField, field_id)
            assert row.state == FIELD_PENDING, "still there to approve against the new version"
        finally:
            check.close()
    finally:
        _purge(booking_type, booking_id)


def test_both_regenerate_routes_read_the_document_under_a_lock():
    """The race tests above call the locking helper directly, so they prove
    the pattern but would not notice the ROUTE going back to an unlocked
    read -- which is exactly the regression that caused both silent losses.
    Read it out of the source rather than retyping the rule (the same shape
    as the event-type literal test)."""
    import inspect

    from app.api import admin_bookings

    for fn in (admin_bookings.generate_document, admin_bookings.generate_document_confirmed):
        source = inspect.getsource(fn)
        assert "lock_current_for_update" in source, fn.__name__
        assert "documents_service.get_current(" not in source, (
            f"{fn.__name__} decides from the document and then writes; an unlocked read there "
            "destroys an approval that lands in between"
        )


# --- a regenerate says so BEFORE it invalidates pending work -------------------
#
# Aaron, 2026-09-06: "If a regenerate silently invalidates pending work,
# I'll hit exactly the error you just built, without knowing why. Tell me
# before, not after."


def test_a_regenerate_warns_that_a_pending_proposal_will_need_re_approving(admin_client, db, loft):
    booking = _booking(db, loft, name="Pending Warn")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).")
    beo_proposals.propose(
        db, booking, fields={"room_layout_notes": "Rounds of 8, dance floor centre."},
        source="client email 6 Sep", actor="ai:claude",
    )
    db.expire_all()

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code == 409
    assert "not been reviewed yet" in resp.text
    assert "Room layout notes" in resp.text
    assert "approving one now would fail" in resp.text.lower()


def test_pending_work_alone_is_enough_to_stop_a_regenerate(admin_client, db, loft):
    """The case with nothing to lose shows no screen at all, so without this
    it is the ONE path that invalidates pending work in total silence."""
    booking = _booking(db, loft, name="Pending Only")
    _beo(db, booking)  # placeholders only -- no losses
    beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="client email 6 Sep", actor="ai:claude",
    )
    db.expire_all()

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code == 409
    assert "would invalidate work you have not reviewed yet" in resp.text
    assert "Regenerate anyway" in resp.text
    db.expire_all()
    assert documents_service.get_current(db, booking.id, DocumentType.beo).version == 1


def test_regenerating_anyway_goes_through_and_leaves_the_proposal_to_re_approve(admin_client, db, loft):
    booking = _booking(db, loft, name="Pending Through")
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="client email 6 Sep", actor="ai:claude",
    )
    db.expire_all()
    csrf = _csrf(admin_client, booking.id)
    shown = _regenerate(admin_client, booking.id, csrf)
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect},
    )

    assert resp.status_code in (200, 303)
    db.expire_all()
    assert documents_service.get_current(db, booking.id, DocumentType.beo).version == 2
    # Not lost -- still there, still pending, to be approved against v2.
    row = db.query(BeoProposalField).filter_by(proposal_id=proposal.id).one()
    assert row.state == FIELD_PENDING
    assert beo_proposals.approve_field(db, row, actor="staff:aaron") is not None


def test_a_booking_with_no_proposal_sees_no_warning(admin_client, db, loft):
    booking = _booking(db, loft, name="No Proposal")
    _beo(db, booking, dietaries="1x severe nut allergy (table 4).")

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))

    assert resp.status_code == 409
    assert "not been reviewed yet" not in resp.text


# --- ultrareview 2026-09-06: the three findings ---------------------------------


MERGED = "DJ 8pm + Magician from 9pm"


def _legacy_beo(db, booking):
    """An older Event Order: one merged music/entertainment value and no
    split fields, which is what every pre-wizard document looks like."""
    return _beo(db, booking, music=None, entertainment=None, music_entertainment=MERGED)


def test_approving_music_alone_on_a_legacy_event_order_is_refused(db, loft):
    """The template prints the merged value under Music until a split
    `music` exists; approving Music alone clears it, and the magician was
    gone from the printed run sheet."""
    booking = _booking(db, loft, name="Legacy Music")
    _legacy_beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"music": "DJ 8pm"}, source="client email", actor="ai:claude"
    )
    assert result.blocked
    assert beo_rules.LEGACY_MUSIC_SPLIT in result.codes
    # The merged value travels as the violation's excerpt -- that is what
    # the 422 hands the model, so it can see exactly what it must carry.
    assert [v.excerpt for v in result.violations if v.code == beo_rules.LEGACY_MUSIC_SPLIT] == [MERGED]
    assert "Entertainment" in result.as_note()


@pytest.mark.parametrize("fields", [
    {"music": "DJ 8pm", "entertainment": "Magician from 9pm"},   # the split, done properly
    {"music": MERGED},                                          # Music carries the whole value
    {"entertainment": "Magician from 9pm"},                     # Entertainment on its own is never lossy
])
def test_a_legacy_event_order_accepts_a_split_that_loses_nothing(db, loft, fields):
    booking = _booking(db, loft, name="Legacy Music OK")
    _legacy_beo(db, booking)
    _, result = beo_proposals.propose(db, booking, fields=fields, source="client email", actor="ai:claude")
    assert not result.blocked, result.codes


def test_the_legacy_rule_ignores_the_generators_own_placeholder(db, loft):
    """Every freshly generated Event Order carries the [REVIEW] prompt in
    the merged field. That is not a value anyone wrote, and counting it
    refused every proposal that touched Music (found when the rule first
    landed: four tests, one cause)."""
    booking = _booking(db, loft, name="Placeholder Music")
    doc = _beo(db, booking)
    assert doc.content["music_entertainment"].startswith("[REVIEW]")
    _, result = beo_proposals.propose(db, booking, fields={"music": "DJ 8pm"}, source="client email", actor="ai:claude")
    assert not result.blocked, result.codes


def test_the_legacy_rule_is_dormant_once_music_is_split(db, loft):
    """A merged value behind a split `music` is not printed, so clearing it
    loses nothing visible."""
    booking = _booking(db, loft, name="Split Already")
    _beo(db, booking, music="Playlist.", entertainment=None, music_entertainment=MERGED)
    _, result = beo_proposals.propose(db, booking, fields={"music": "DJ 8pm"}, source="client email", actor="ai:claude")
    assert not result.blocked, result.codes


def test_the_legacy_rule_runs_at_approval_too(db, loft):
    """The box is editable: a clean proposal can be edited into the lossy
    shape at the moment of approval, so the rule must run there."""
    booking = _booking(db, loft, name="Legacy At Approval")
    _legacy_beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"music": MERGED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked
    with pytest.raises(beo_proposals.ProposalError, match="Entertainment"):
        beo_proposals.approve_field(db, proposal.fields[0], actor="staff:aaron", value="DJ 8pm")
    db.expire_all()
    current = beo_proposals.current_draft_beo(db, booking.id)
    assert current.content["music_entertainment"] == MERGED, "nothing was cleared"


def test_approving_entertainment_first_then_music_is_the_documented_way_through(db, loft):
    booking = _booking(db, loft, name="Legacy Two Step")
    _legacy_beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"music": "DJ 8pm", "entertainment": "Magician from 9pm"},
        source="client email", actor="ai:claude",
    )
    assert not result.blocked
    rows = {f.field: f for f in proposal.fields}
    beo_proposals.approve_field(db, rows["entertainment"], actor="staff:aaron")
    beo_proposals.approve_field(db, rows["music"], actor="staff:aaron")
    db.expire_all()
    content = beo_proposals.current_draft_beo(db, booking.id).content
    assert content["music"] == "DJ 8pm"
    assert content["entertainment"] == "Magician from 9pm"
    assert content["music_entertainment"] is None


def test_an_approval_is_not_reported_as_a_hand_edit(admin_client, db, loft, staff_user):
    """Approvals went through update_content_fields, which wrote the same
    document_edited event a hand-edit does, so the regenerate screen told
    Aaron the draft was hand-edited by the approver -- next to a badge
    saying the same value was approved by the approver."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Not A Hand Edit")
    doc = _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="client email 6 Sep", actor="ai:claude",
    )
    beo_proposals.approve_field(db, proposal.fields[0], actor=f"staff:{staff_user.email}")
    db.expire_all()

    assert dr.was_hand_edited(db, doc) is None
    applied = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="beo_proposal_applied").count()
    assert applied == 1

    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))
    assert resp.status_code == 409
    assert "approved by" in resp.text
    assert "hand-edited" not in resp.text


def test_a_real_hand_edit_is_still_reported(admin_client, db, loft):
    booking = _booking(db, loft, name="Real Hand Edit")
    doc = _beo(db, booking)
    documents_service.update_content_fields(
        db, doc, {"dietaries": "1x severe nut allergy (table 4)."}, actor="staff:aaron"
    )
    db.expire_all()
    resp = _regenerate(admin_client, booking.id, _csrf(admin_client, booking.id))
    assert resp.status_code == 409
    assert "hand-edited by staff:aaron" in resp.text


def test_a_proposal_resolved_by_an_approval_is_not_relabelled_superseded(db, loft):
    """The parent row is compare-and-set as well as the fields. A proposal
    whose every field was approved is RESOLVED, and a later proposal must
    not rewrite that to SUPERSEDED: it superseded nothing."""
    booking = _booking(db, loft, name="Resolved Stays")
    _beo(db, booking)
    first, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="email one", actor="ai:claude",
    )
    beo_proposals.approve_field(db, first.fields[0], actor="staff:aaron")
    db.expire_all()
    assert first.status == STATUS_RESOLVED

    second, _ = beo_proposals.propose(db, booking, fields={"music": "DJ 8pm"}, source="email two", actor="ai:claude")
    db.expire_all()
    assert first.status == STATUS_RESOLVED
    assert first.fields[0].state == FIELD_APPROVED
    assert second.status == STATUS_PENDING
    assert db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="beo_proposal_superseded").count() == 0


# --- the review of "a proposal creates the draft" (2026-09-11) ---------------------


def test_an_enquirys_proposal_is_stored_but_makes_no_draft(ai_client, db, loft):
    """An enquiry's Event Order is a staff decision, as it is for a client
    who has not signed or paid. The proposal waits, and the answer says so."""
    booking = _booking(db, loft)  # status: enquiry

    resp = _propose(ai_client, booking, CLEAN)

    assert resp.status_code == 201
    assert resp.json()["event_order"] is None
    assert "still an enquiry" in resp.json()["note"]
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None


def test_a_proposal_the_drafts_own_content_would_block_creates_nothing(db, loft, menu_items):
    """Judged BEFORE the draft exists, against what it would hold. A
    submitted wizard's dietaries carry the client's allergy; a proposal
    that drops it is blocked -- and no draft is left behind."""
    from tests.test_wizard_generation import _complete_all_steps, _make_booking, _pay_deposit
    from app.models.wizard_session import WizardSessionStatus
    from app.services import wizard as wizard_service

    booking = _confirmed(db, _make_booking(db, loft))
    _pay_deposit(db, booking)
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test")
    _complete_all_steps(db, session, menu_items)
    session.extras_response = {**(session.extras_response or {}), "dietary_requirements": "1x severe nut allergy"}
    session.status = WizardSessionStatus.submitted
    db.commit()
    assert "nut allergy" in beo_proposals.fresh_beo_content(db, booking)["dietaries"]

    proposal, result = beo_proposals.propose(
        db, booking, fields={"dietaries": "No dietary requirements"}, source="email", actor="ai:claude"
    )

    assert result.blocked and "drops_dietary" in result.codes, result.codes
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None, "a blocked proposal creates nothing"
    assert proposal.document_id is None


def test_a_proposal_on_a_sent_event_order_is_judged_against_it_and_waits_for_a_revise(ai_client, db, loft):
    """Not against blank values: a Revise copies the sent version forward,
    and approval will judge against that. So a proposal that would drop
    the sent version's allergy is refused NOW, and a clean one is told a
    Revise is needed."""
    booking = _booking(db, loft)
    sent = documents_service.mark_sent(db, _beo(db, booking, dietaries="1x severe nut allergy"), actor="staff:test")

    dropped = _propose(ai_client, booking, {"dietaries": "No dietary requirements"})
    clean = _propose(ai_client, booking, {"music": "DJ from 8pm"})

    assert dropped.status_code == 422 and "drops_dietary" in dropped.json()["detail"]["rule_codes"]
    assert clean.status_code == 201
    assert clean.json()["event_order"] == {"version": 1, "status": "sent", "created": False}
    assert "must Revise it" in clean.json()["note"]
    assert documents_service.get_current(db, booking.id, DocumentType.beo).id == sent.id


def test_a_draft_created_by_a_proposal_does_not_reset_the_pipeline_clock(db, loft):
    """days_at_stage measures people: a document_created written by an
    `ai:` actor (a proposal making the first draft) must not restart the
    clock; the same event by staff does. Rows are inserted with explicit
    times because inside one test transaction every now() is identical."""
    from app.services import ai_pipeline

    booking = _confirmed(db, _booking(db, loft))
    base = ai_pipeline.compute_stage_since(booking)
    later = base + dt.timedelta(days=3)
    db.add(BookingEvent(booking_id=booking.id, event_type="document_created", field_name="beo_version", new_value="1", actor="ai:claude", created_at=later))
    db.commit()
    db.expire(booking)

    assert ai_pipeline.compute_stage_since(booking) == base, "an AI-made draft is not the booking moving"

    db.add(BookingEvent(booking_id=booking.id, event_type="document_created", field_name="beo_version", new_value="2", actor="staff:aaron", created_at=later + dt.timedelta(days=1)))
    db.commit()
    db.expire(booking)

    assert ai_pipeline.compute_stage_since(booking) == later + dt.timedelta(days=1), "a staff Generate is"

def test_a_draft_created_by_a_proposal_does_not_clear_the_notes_review_finding(db, hamilton, loft):
    from app.services import reconciliation

    booking = _confirmed(db, _booking(db, loft))
    booking.notes = "client said the cousin is allergic to everything, check"
    db.commit()
    assert "NOTES_BEFORE_BEO" in {f.check_code for f in reconciliation.collect(db, hamilton) if f.booking_id == booking.id}

    beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")

    assert "NOTES_BEFORE_BEO" in {f.check_code for f in reconciliation.collect(db, hamilton) if f.booking_id == booking.id}, "nobody has read the notes yet"
    documents_service.create_new_version(db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test")
    assert "NOTES_BEFORE_BEO" not in {f.check_code for f in reconciliation.collect(db, hamilton) if f.booking_id == booking.id}


def test_the_booking_page_notice_is_worded_from_the_event_orders_state(admin_client, db, loft):
    enquiry = _booking(db, loft, name="Notice Enquiry")
    beo_proposals.propose(db, enquiry, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    page = admin_client.get(f"/admin/bookings/{enquiry.id}").text
    assert "Generate the Event Order to review them" in page

    sent_booking = _booking(db, loft, name="Notice Sent")
    documents_service.mark_sent(db, _beo(db, sent_booking), actor="staff:test")
    beo_proposals.propose(db, sent_booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    page = admin_client.get(f"/admin/bookings/{sent_booking.id}").text
    assert "(v1, sent) has already gone out" in page and "Revise it" in page

    legacy_booking = _booking(db, loft, name="Notice Legacy")
    legacy = _beo(db, legacy_booking)
    legacy.is_legacy = True
    legacy.status = DocumentStatus.signed
    db.commit()
    beo_proposals.propose(db, legacy_booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    page = admin_client.get(f"/admin/bookings/{legacy_booking.id}").text
    assert "legacy record" in page


def test_a_staff_generate_and_a_proposal_racing_for_the_first_draft_make_one_draft():
    """Two real sessions. The staff Generate's own sequence (booking-row
    lock, locked read, create) takes the lock and HOLDS it for a second
    before creating; propose() starts inside that second. With the
    booking-row lock in propose it waits and then sees the staff draft;
    without it, it saw None, created v1 first, and the staff create hit
    the unique index (proved 2026-09-11). One v1, the proposal attached
    to it, no error, and the winner is always the staff side here."""
    import time

    from sqlalchemy import text as sql_text

    from app.models import Booking
    from app.models.booking import BookingStatus
    from app.models.document import Document
    from app.seed import seed as seed_hamilton
    from app.services.booking import change_status, create_booking
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    space = next(sp for sp in venue.spaces if sp.is_bookable)
    contact = Contact(name="Race First Draft", email=f"race.first.{uuid.uuid4().hex[:8]}@example.com")
    setup.add(contact)
    setup.flush()
    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=f"Race First {uuid.uuid4().hex[:6]}",
        event_type="corporate", adult_count=40, child_count=0, notes=None, actor="test",
    )
    change_status(setup, booking, BookingStatus.confirmed, actor="test")
    booking_id = booking.id
    setup.close()

    staff_holds_the_lock = threading.Event()
    errors = {}
    outcome = {}

    def staff_generate():
        session = TestSessionLocal()
        try:
            b = session.get(Booking, booking_id)
            content = beo_proposals.fresh_beo_content(session, b)
            documents_service.lock_booking_row(session, booking_id)
            current = documents_service.lock_current_for_update(session, booking_id, DocumentType.beo)
            assert current is None
            staff_holds_the_lock.set()
            time.sleep(1.0)  # the AI's propose is running now
            documents_service.create_new_version(session, b, DocumentType.beo, content, actor="staff:generate")
            outcome["staff"] = "created"
        except Exception as exc:  # noqa: BLE001
            errors["staff"] = exc
        finally:
            session.close()

    def ai_propose():
        session = TestSessionLocal()
        try:
            b = session.get(Booking, booking_id)
            assert staff_holds_the_lock.wait(timeout=5)
            started = time.monotonic()
            proposal, result = beo_proposals.propose(session, b, fields={"dietaries": "1x GF"}, source="race", actor="ai:claude")
            outcome["ai"] = (proposal.document_id, bool(getattr(proposal, "draft_created", False)), result.blocked, time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001
            errors["ai"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=staff_generate), threading.Thread(target=ai_propose)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = TestSessionLocal()
    try:
        assert not errors, errors
        docs = check.query(Document).filter_by(booking_id=booking_id).all()
        assert [(d.version, d.is_current) for d in docs] == [(1, True)], (outcome, [(d.version, d.is_current) for d in docs])
        document_id, created, blocked, waited = outcome["ai"]
        assert outcome["staff"] == "created" and not created, ("the AI waited and took the staff draft", outcome)
        assert not blocked and document_id == docs[0].id, outcome
        assert waited >= 0.5, f"propose did not wait on the lock ({waited:.2f}s)"
    finally:
        check.execute(sql_text("SET LOCAL app.allow_booking_purge='on'"))
        target = check.get(Booking, booking_id)
        if target is not None:
            from app.services.booking import delete_booking_and_dependents
            delete_booking_and_dependents(check, target, actor="staff:test")
        check.close()
def test_the_draft_creator_never_builds_over_a_version_that_exists(db, loft):
    """The race-only branch of _create_draft_for_proposal: propose() only
    calls it when no Event Order exists, but a staff Generate can land
    between that read and the lock. Whatever it then finds: a draft is
    returned as-is, a sent (or viewed, or approved) version is left alone,
    and nothing is created either way."""
    booking = _confirmed(db, _booking(db, loft))
    content = beo_proposals.fresh_beo_content(db, booking)

    draft = _beo(db, booking)
    assert beo_proposals._create_draft_for_proposal(db, booking, actor="ai:claude", content=content) == (draft, False)

    documents_service.mark_sent(db, draft, actor="staff:test")
    assert beo_proposals._create_draft_for_proposal(db, booking, actor="ai:claude", content=content) == (None, False)
    assert documents_service.get_current(db, booking.id, DocumentType.beo).id == draft.id


def test_a_failure_after_the_draft_leaves_no_draft_behind(db, loft, monkeypatch):
    """The draft and the proposal are one transaction: if the proposal
    cannot be written, the draft it made is rolled back with it. (With
    create_new_version committing on its own, an AI-made draft with no
    proposal was left behind and the retry answered created=False.)"""
    booking = _confirmed(db, _booking(db, loft))

    def explode(*args, **kwargs):
        raise RuntimeError("supersede failed")

    monkeypatch.setattr(beo_proposals, "_supersede_older", explode)
    with pytest.raises(RuntimeError):
        beo_proposals.propose(db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude")
    db.rollback()

    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None
    assert not [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all() if e.event_type == "beo_draft_by_proposal"]


# =============================================================================
# The food order as catalogue items and quantities (Aaron, 2026-09-11)
#
# "Let the AI propose the food order as catalogue items and quantities. Not
# prices, not custom lines. ... The AI never sends a figure, so it cannot get
# one wrong. It can get an item or a quantity wrong, and that's what approval
# is for."
# =============================================================================

import json as _json


def _food(menu_items, *pairs):
    """[(name, qty), ...] -> the selection the AI sends, by id."""
    return [{"menu_item_id": str(menu_items[name].id), "quantity": qty} for name, qty in pairs]


def _food_row(proposal):
    return next(f for f in proposal.fields if f.field == "food_order")


def test_a_food_order_is_priced_from_the_catalogue_and_stored_without_prices(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    proposal, result = beo_proposals.propose(
        db, booking, fields={}, source="client email 10 Sep",
        actor="ai:claude", food_order=[{"name": "grazing  platter", "quantity": 2}] + _food(menu_items, ("Pork Belly Bites", 3)),
    )

    assert not result.blocked, result.codes
    row = _food_row(proposal)
    assert row.state == FIELD_PENDING
    stored = _json.loads(row.proposed_value)
    assert stored == [
        {"menu_item_id": str(menu_items["Grazing Platter"].id), "quantity": 2},
        {"menu_item_id": str(menu_items["Pork Belly Bites"].id), "quantity": 3},
    ], "catalogue ids and quantities, in the order proposed -- no names (a rename is not an edit), no prices"
    rows = beo_proposals.review_rows(db, booking.id)
    food = next(r for r in rows if r["field"] == "food_order")
    assert food["kind"] == "food" and food["label"] == "Food order"
    assert [(ln["name"], ln["quantity"], ln["unit_price"], ln["line_total"]) for ln in food["lines"]] == [
        ("Grazing Platter", 2, "250.00", "500.00"), ("Pork Belly Bites", 3, "100.00", "300.00")
    ]
    assert food["total"] == "800.00"
    assert food["problems"] == []


def test_a_food_line_carrying_a_price_is_refused_by_name(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    proposal, result = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=[{"name": "Grazing Platter", "quantity": 1, "unit_price": "1.00"}],
    )

    assert result.blocked and result.codes == ["food_price_sent"]
    assert "never writes a price" in result.as_note()
    assert proposal.status == STATUS_RULES_BLOCKED
    assert documents_service.get_current(db, booking.id, DocumentType.beo) is None, "a blocked proposal makes no draft"


def test_an_unknown_or_retired_item_is_refused_with_the_active_items_listed(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    _, retired = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=[{"name": "Dessert Platter", "quantity": 1}]
    )
    _, bogus = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=[{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]
    )

    assert retired.codes == ["food_unknown_item"], "retired items are not on offer"
    assert "Grazing Platter (platter)" in retired.as_note(), "the refusal lists what can be ordered"
    assert bogus.codes == ["food_unknown_item"]


@pytest.mark.parametrize("quantity", [0, 501, "2", True, 2.5, None])
def test_a_quantity_is_a_whole_number_from_one_to_five_hundred(db, loft, menu_items, quantity):
    booking = _confirmed(db, _booking(db, loft))

    _, result = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=[{"name": "Grazing Platter", "quantity": quantity}],
    )

    assert result.codes == ["food_bad_quantity"], (quantity, result.codes)


def test_one_line_per_item(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    _, result = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 1), ("Grazing Platter", 2)),
    )

    assert result.codes == ["food_duplicate_item"]


def test_a_legacy_priced_booking_prices_pizzas_at_the_legacy_price_and_refuses_what_has_none(db, loft, menu_items):
    """The catalogue's own rule, exactly as the wizard applies it: a booking
    locked before the pizza cutover pays the legacy price, and an item
    with no legacy price on record is a refusal, never a guess."""
    from tests.test_wizard_generation import _make_booking

    booking = _make_booking(db, loft, created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))

    _, priced = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Margherita Pizza", 4))
    )
    rows = beo_proposals.review_rows(db, booking.id)
    _, refused = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Vegetarian Pizza", 1))
    )

    assert not priced.blocked
    assert next(r for r in rows if r["field"] == "food_order")["lines"][0]["unit_price"] == "26.00"
    assert refused.codes == ["food_price_unavailable"]


def test_approving_the_food_order_writes_the_lines_and_the_total_from_the_catalogue(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3), ("Tiramisu Cake", 1)),
    )

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    lines = document.content["food_order"]["line_items"]
    # Plus `source`: the sync's ownership mark, distinct from the id since
    # 2026-09-14 when the wizard started writing ids too. Only lines the
    # sync writes carry it, and it is what _is_catalogue_built reads.
    own = beo_proposals.LINE_SOURCE_PROPOSAL
    assert lines == [
        {"description": "Grazing Platter", "quantity": 2, "unit_price": "250.00", "category": "platter", "menu_item_id": str(menu_items["Grazing Platter"].id), "source": own},
        {"description": "Pork Belly Bites", "quantity": 3, "unit_price": "100.00", "category": "platter", "menu_item_id": str(menu_items["Pork Belly Bites"].id), "source": own},
        {"description": "Tiramisu Cake", "quantity": 1, "unit_price": "80.00", "category": "dessert", "menu_item_id": str(menu_items["Tiramisu Cake"].id), "source": own},
    ], "the shape every reader of food_order knows, plus the item id and the sync's mark; a cake prints under Desserts"
    assert document.content["food_order"]["note"] is None
    assert document.content["total_food_spend"]["total"] == "880.00", "the heading is rebuilt with the lines"
    assert "food_order" in document.content["_authored"], "an approval is a person putting these lines on the document"
    events = {e.event_type: e for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all() if e.field_name == "food_order"}
    assert events["beo_proposal_approved"].new_value == "2 x Grazing Platter @ 250.00; 3 x Pork Belly Bites @ 100.00; 1 x Tiramisu Cake @ 80.00 = 880.00"
    assert "beo_proposal_edited" not in events
    assert _json.loads(_food_row(proposal).applied_value) == _json.loads(_food_row(proposal).proposed_value)


def test_changing_a_quantity_before_approving_is_recorded_and_zero_removes_the_line(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3)),
    )
    edited = _json.dumps([
        {"menu_item_id": str(menu_items["Grazing Platter"].id), "quantity": 1},
        {"menu_item_id": str(menu_items["Pork Belly Bites"].id), "quantity": 0},
    ])

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron", value=edited)

    assert [(ln["description"], ln["quantity"]) for ln in document.content["food_order"]["line_items"]] == [("Grazing Platter", 1)]
    assert document.content["total_food_spend"]["total"] == "250.00"
    row = _food_row(proposal)
    assert row.edited_before_approval
    edited_events = [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="beo_proposal_edited").all()]
    assert [e.field_name for e in edited_events] == ["food_order"]
    # The "before" is what was PROPOSED, read from the stored selection and
    # priced by nobody -- so a line dropped at approval is visible in it.
    assert edited_events[0].old_value == "2 x Grazing Platter; 3 x Pork Belly Bites"
    assert edited_events[0].new_value.endswith("= 250.00")


def test_zeroing_every_line_is_refused_rather_than_approving_nothing(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    with pytest.raises(beo_proposals.ProposalError) as exc:
        beo_proposals.approve_field(
            db, _food_row(proposal), actor="staff:aaron",
            value=_json.dumps([{"menu_item_id": str(menu_items["Grazing Platter"].id), "quantity": 0}]),
        )

    assert "reject the food order" in str(exc.value)
    assert _food_row(proposal).state == FIELD_PENDING


def test_approve_all_covers_the_text_fields_and_the_food_order_together(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields=CLEAN, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    document = beo_proposals.approve_all(db, proposal, actor="staff:aaron")

    assert document.version == 1
    assert document.content["dietaries"] == CLEAN["dietaries"]
    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter"
    assert proposal.status == STATUS_RESOLVED
    assert {f.field: f.state for f in proposal.fields} == {
        "catering_order_and_service_style": FIELD_APPROVED, "dietaries": FIELD_APPROVED, "food_order": FIELD_APPROVED
    }


def test_an_item_retired_after_the_proposal_refuses_the_approval_and_the_panel_says_so(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    menu_items["Grazing Platter"].is_active = False
    db.commit()

    rows = beo_proposals.review_rows(db, booking.id)
    with pytest.raises(beo_proposals.ProposalError) as exc:
        beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert next(r for r in rows if r["field"] == "food_order")["problems"], "the reviewer is told before clicking"
    assert "not an active catalogue item" in str(exc.value)


def test_an_approved_food_order_is_protected_from_a_silent_regenerate(db, loft, menu_items):
    from app.services import document_regeneration

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    current = documents_service.get_current(db, booking.id, DocumentType.beo)

    losses = document_regeneration.losses(db, current, generate_beo_content(booking))

    assert losses, "a regenerate over an approved food order must stop and ask"
    assert "Grazing Platter" in str(losses)


def test_the_endpoint_accepts_a_food_order_and_reports_it_pending(ai_client, db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    resp = ai_client.post(
        f"/api/ai/bookings/{booking.reference_code}/event-order-proposal",
        json={"source": "client email 10 Sep, final details", "food_order": [{"name": "Grazing Platter", "quantity": 2}]},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["awaiting_approval"] == ["food_order"]
    assert body["event_order"]["created"] is True


def test_the_endpoint_refuses_a_priced_line_with_its_code(ai_client, db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    resp = ai_client.post(
        f"/api/ai/bookings/{booking.reference_code}/event-order-proposal",
        json={"source": "client email", "food_order": [{"name": "Grazing Platter", "quantity": 2, "unit_price": "9.00"}]},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["rule_codes"] == ["food_price_sent"]
    assert "food_order" in resp.json()["detail"]


def test_a_proposal_with_neither_text_nor_food_is_refused(ai_client, db, loft):
    booking = _confirmed(db, _booking(db, loft))

    resp = ai_client.post(
        f"/api/ai/bookings/{booking.reference_code}/event-order-proposal", json={"source": "client email", "fields": {}}
    )

    assert resp.status_code == 422
    assert "or a food_order" in resp.json()["detail"], "refused before the rules, with the food shape named"
    assert beo_proposals.latest_proposal(db, booking.id) is None, "nothing written for calibration -- there was nothing to calibrate"


def test_the_panel_shows_the_food_lines_with_catalogue_prices_and_a_total(admin_client, db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3)),
    )
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{draft.id}/edit").text

    panel = page[page.index("Proposed by Claude"):]
    assert "Food order" in panel and "Grazing Platter" in panel and "$250.00" in panel and "$800.00" in panel
    assert 'name="food_quantities"' in panel and 'name="food_item_ids"' in panel
    assert "never proposable" not in panel, "the copy no longer says the food order cannot be proposed"


def test_approving_from_the_panel_with_a_changed_quantity(admin_client, db, loft, menu_items):
    from tests.test_staff_app import _csrf_of

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x GF"}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3)),
    )
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)
    csrf = _csrf_of(admin_client, f"/admin/bookings/{booking.id}/documents/{draft.id}/edit")

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={
            "csrf_token": csrf, "action": "approve_all", "value_dietaries": "1x GF",
            "food_item_ids": [str(menu_items["Grazing Platter"].id), str(menu_items["Pork Belly Bites"].id)],
            "food_quantities": ["1", "0"],
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    db.refresh(draft)
    assert [(ln["description"], ln["quantity"]) for ln in draft.content["food_order"]["line_items"]] == [("Grazing Platter", 1)]
    assert draft.content["dietaries"] == "1x GF"
    assert draft.content["total_food_spend"]["total"] == "250.00"


# --- the review of the food order (2026-09-11) -------------------------------------
#
# Every one of these was reproduced before it was fixed. The first is the
# one that mattered: the panel said "Cannot be approved as it stands" and
# approving from it succeeded with a $300 line gone.


def test_approving_from_the_panel_cannot_drop_a_line_that_can_no_longer_be_priced(admin_client, db, loft, menu_items):
    from tests.test_staff_app import _csrf_of

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3)),
    )
    menu_items["Pork Belly Bites"].is_active = False
    db.commit()
    draft = documents_service.get_current(db, booking.id, DocumentType.beo)
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{draft.id}/edit").text
    panel = page[page.index("Proposed by Claude"):]

    # Every proposed line gets a row, so the form cannot post a subset.
    assert panel.count('name="food_item_ids"') == 2
    assert "no longer in the catalogue" in panel
    assert str(menu_items["Pork Belly Bites"].id) in panel

    csrf = _csrf_of(admin_client, f"/admin/bookings/{booking.id}/documents/{draft.id}/edit")
    subset = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={"csrf_token": csrf, "action": "approve_all",
              "food_item_ids": [str(menu_items["Grazing Platter"].id)], "food_quantities": ["2"]},
    )

    assert subset.status_code == 409, "approving a subset of a proposal is a refusal"
    assert "left out 3 x Pork Belly Bites" in subset.text
    db.refresh(draft)
    assert (draft.content.get("food_order") or {}).get("line_items") in (None, []), "nothing was written"
    assert _food_row(proposal).state == FIELD_PENDING

    # The whole proposal, with the unpriceable line explicitly zeroed.
    both = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={"csrf_token": csrf, "action": "approve_all",
              "food_item_ids": [str(menu_items["Grazing Platter"].id), str(menu_items["Pork Belly Bites"].id)],
              "food_quantities": ["2", "0"]},
        follow_redirects=False,
    )

    assert both.status_code == 303, both.text
    db.refresh(draft)
    assert [(ln["description"], ln["quantity"]) for ln in draft.content["food_order"]["line_items"]] == [("Grazing Platter", 2)]


def test_a_retired_line_already_on_the_event_order_can_be_re_proposed_and_kept(db, loft, menu_items):
    """An approval REPLACES the food order, so a line it cannot name is a
    line it drops. Retirement means "no longer offered", never "your
    existing order is now unpriceable" -- the wizard's own rule."""
    booking = _confirmed(db, _booking(db, loft))
    content = generate_beo_content(booking)
    content["food_order"] = {"line_items": [{
        "description": "Dessert Platter", "quantity": 1, "unit_price": "140.00", "category": "dessert",
        "menu_item_id": str(menu_items["Dessert Platter"].id),
    }], "note": None}
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")
    assert menu_items["Dessert Platter"].is_active is False

    proposal, result = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Dessert Platter", 1), ("Grazing Platter", 2)),
    )

    assert not result.blocked, result.codes
    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    assert [(ln["description"], ln["quantity"], ln["unit_price"]) for ln in document.content["food_order"]["line_items"]] == [
        ("Dessert Platter", 1, "140.00"), ("Grazing Platter", 2, "250.00")
    ], "the line the client already ordered keeps its name and its quoted price"


def test_a_retired_item_not_already_ordered_is_still_refused(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))

    _, result = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Dessert Platter", 1))
    )

    assert result.codes == ["food_unknown_item"], "a NEW selection is active items only"


def test_the_deposit_on_the_total_block_is_the_wizards_rule(db, loft, menu_items):
    """0.00 is a fact. It used to be written as None with a note saying
    payments are not tracked in Concierge -- over a figure this had just
    read from the payments."""
    from tests.test_wizard_generation import _pay_deposit

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert document.content["total_food_spend"] == {
        "total": "500.00", "deposit_paid": "0.00", "balance_due": "500.00", "note": None
    }

    _pay_deposit(db, booking)
    second, _ = beo_proposals.propose(
        db, booking, fields={}, source="email 2", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 3))
    )
    document = beo_proposals.approve_field(db, _food_row(second), actor="staff:aaron")

    assert document.content["total_food_spend"] == {
        "total": "750.00", "deposit_paid": "500.00", "balance_due": "250.00", "note": None
    }


def test_a_catalogue_rename_between_propose_and_approve_is_not_an_edit(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    menu_items["Grazing Platter"].name = "Grazing Platter (large)"
    db.commit()

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert _food_row(proposal).edited_before_approval is False, "nobody edited anything; the catalogue was renamed"
    assert not [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="beo_proposal_edited").all()]
    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter (large)", "priced and named now"


def test_the_edit_form_keeps_the_catalogue_id_and_a_no_op_save_changes_nothing(admin_client, db, loft, menu_items):
    """The id is what makes a line recognisable as the catalogue's -- the
    invoice is built from these. It used to be stripped by the first save,
    which then audited every save as 'changed the food order'."""
    import re

    from tests.test_staff_app import _csrf_of

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    draft = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{draft.id}/edit").text
    assert f'name="item_menu_item_ids" value="{menu_items["Grazing Platter"].id}"' in page
    # And the ownership mark beside it, on the same terms: rendered into a
    # hidden input so a no-op save hands it back. Dropping it would make
    # the stored dict differ from the posted one, which records the food
    # order as a person's -- and refresh_draft_food_prices would then
    # freeze a generated price at send.
    assert f'name="item_sources" value="{beo_proposals.LINE_SOURCE_PROPOSAL}"' in page

    data = {
        "csrf_token": _csrf_of(admin_client, f"/admin/bookings/{booking.id}/documents/{draft.id}/edit"),
        "content_expect": re.search(r'name="content_expect" value="([^"]+)"', page).group(1),
        "item_descriptions": "Grazing Platter", "item_categories": "platter",
        "item_quantities": "2", "item_unit_prices": "250.00",
        "item_menu_item_ids": str(menu_items["Grazing Platter"].id),
        "item_sources": beo_proposals.LINE_SOURCE_PROPOSAL,
    }
    for name in ("catering_order_and_service_style", "bar_structure", "room_layout_notes", "music", "entertainment",
                 "dietaries", "accessibility", "decorations", "special_notes", "onsite_contact", "internal_notes",
                 "status_text"):
        value = draft.content.get(name)
        data[name] = value if isinstance(value, str) else ""

    resp = admin_client.post(f"/admin/bookings/{booking.id}/documents/{draft.id}/edit", data=data, follow_redirects=False)

    assert resp.status_code == 303, resp.text
    db.refresh(draft)
    assert draft.content["food_order"]["line_items"][0]["menu_item_id"] == str(menu_items["Grazing Platter"].id)
    edits = [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="document_edited").all()]
    assert "food_order" not in (edits[-1].old_value or ""), f"a no-op save said it changed the food order: {edits[-1].old_value}"


def test_the_regenerate_screen_says_who_approved_the_food_order(db, loft, menu_items):
    """The one screen whose job is saying WHO put a value there showed the
    AI-approved food order as anonymous, while the text fields approved in
    the same click carried the badge."""
    from app.services import document_regeneration

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy"}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2)),
    )
    beo_proposals.approve_all(db, proposal, actor="staff:aaron")
    current = documents_service.get_current(db, booking.id, DocumentType.beo)

    losses = document_regeneration.losses(db, current, generate_beo_content(booking))

    by_label = {loss.label: loss for loss in losses}
    assert "Food order" in by_label, [loss.label for loss in losses]
    assert by_label["Food order"].approved_note, "the food loss carries no approval badge"
    assert "staff:aaron" in by_label["Food order"].approved_note
    assert by_label["Dietaries"].approved_note, "and the text field approved in the same click still does"


def test_the_approval_key_is_the_proposals_own_spelling(db, loft, menu_items):
    """The badge is string equality, so the two spellings must be one
    definition in two places -- pinned here rather than trusted."""
    from app.services import document_regeneration

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 1)),
    )
    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert document_regeneration._food_order_approval_key(document.content["food_order"]) == _food_row(proposal).applied_value


def test_a_hand_typed_food_line_has_no_approval_identity(db, loft):
    from app.services import document_regeneration

    assert document_regeneration._food_order_approval_key(
        {"line_items": [{"description": "Something typed", "quantity": 1, "unit_price": "10.00"}]}
    ) == "", "a line with no catalogue id cannot be matched to an approval, and must not be guessed at"


# =============================================================================
# The invoice follows the approved food order (Aaron, 2026-09-11)
# =============================================================================


def _final_invoices(db, booking):
    from app.models.invoice import Invoice, InvoiceStatus, InvoiceType

    return db.query(Invoice).filter(
        Invoice.booking_id == booking.id, Invoice.type == InvoiceType.final, Invoice.status != InvoiceStatus.cancelled
    ).all()


def _invoice_events(db, booking):
    return [e for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all() if e.event_type == "final_invoice_from_beo"]


def test_approving_the_food_order_builds_the_draft_final_invoice_with_the_deposit_credited(db, loft, menu_items):
    from app.services import policy
    from tests.test_wizard_generation import _pay_deposit

    booking = _confirmed(db, _booking(db, loft))
    _pay_deposit(db, booking)  # $500
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude",
        food_order=_food(menu_items, ("Grazing Platter", 2), ("Pork Belly Bites", 3)),
    )

    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    invoices = _final_invoices(db, booking)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.status.value == "draft"
    assert [(ln["description"], ln["quantity"], ln["unit_price"]) for ln in invoice.line_items] == [
        ("Grazing Platter", 2, "250.00"), ("Pork Belly Bites", 3, "100.00"), ("Less: deposit credited", 1, "-500.00")
    ]
    assert invoice.line_items[0]["menu_item_id"] == str(menu_items["Grazing Platter"].id), "a catalogue line is recognisable"
    assert str(invoice.total) == "300.00"
    assert invoice.due_date == policy.final_balance_due_date(booking.event_date, issued_on=dt.date.today())
    assert "built from the approved food order" in _invoice_events(db, booking)[-1].new_value


def test_a_draft_this_sync_built_is_refreshed_not_duplicated(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 1))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    built = _final_invoices(db, booking)[0]
    built.due_date = dt.date(2027, 5, 7)
    db.commit()

    second, _ = beo_proposals.propose(
        db, booking, fields={}, source="email 2", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 3))
    )
    beo_proposals.approve_field(db, _food_row(second), actor="staff:aaron")

    invoices = _final_invoices(db, booking)
    assert [i.id for i in invoices] == [built.id], "refreshed, not duplicated"
    db.refresh(built)
    assert [(ln["description"], ln["quantity"]) for ln in built.line_items] == [("Grazing Platter", 3)]
    assert str(built.total) == "750.00"
    assert built.due_date == dt.date(2027, 5, 7), "the draft's own due date stands"
    assert "refreshed" in _invoice_events(db, booking)[-1].new_value


def test_a_draft_carrying_anything_this_sync_did_not_write_is_left_alone(admin_client, db, loft, menu_items):
    """THE REVIEW'S HIGH FINDING. update_invoice replaces the whole
    charge-line set, so handing it the food alone deleted room hire, a bar
    tab, a negotiated discount -- or the wizard's own priced in-house cake
    -- with no banner and a trail row that read like a success. The
    review's suggested merge (keep every line with no menu_item_id) would
    have double-billed a wizard invoice, whose food lines carry no id
    either; so the rule is narrower: this sync rebuilds only what it
    wrote."""
    from app.services import invoicing

    booking = _confirmed(db, _booking(db, loft))
    staff_built = invoicing.create_final_invoice(
        db, booking,
        line_items=[
            {"description": "Room hire - The Loft", "quantity": 1, "unit_price": "2000.00"},
            {"description": "Goodwill discount", "quantity": 1, "unit_price": "-100.00"},
        ],
        due_date=dt.date(2027, 5, 7), actor="staff:aaron",
    )
    before = (str(staff_built.total), [(ln["description"], ln["unit_price"]) for ln in staff_built.line_items])
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    db.refresh(staff_built)
    assert (str(staff_built.total), [(ln["description"], ln["unit_price"]) for ln in staff_built.line_items]) == before,         "the room hire and the discount are still there"
    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter", "the Event Order still took the lines"
    outcome = _invoice_events(db, booking)[-1].new_value
    assert "carries lines this Event Order did not put there" in outcome
    page = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "did not reach the final invoice" in page and "left alone" in page


def test_a_wizard_built_draft_is_left_alone_rather_than_double_billed(db, loft, menu_items):
    """The wizard's lines are not the sync's to rebuild.

    Until 2026-09-14 that was encoded by the wizard writing NO catalogue
    id, and _is_catalogue_built asking "does every line carry one?". The
    wizard now names its item so a price move can reach its lines, and
    that would have made this invoice look like the sync's -- an approval
    would have rebuilt it and dropped the in-house cake. This test caught
    it. Ownership is its own key now (source=LINE_SOURCE_PROPOSAL), written
    only by the sync, and the wizard's lines never carry it."""
    from app.services import invoicing, policy
    from app.services.wizard_generation import build_food_line_items

    booking = _confirmed(db, _booking(db, loft))
    wizard_lines, _ = build_food_line_items(
        db, booking, {"platters": [{"menu_item_id": str(menu_items["Grazing Platter"].id), "quantity": 2}]}
    )
    assert all(ln.get("menu_item_id") for ln in wizard_lines), "the wizard names its catalogue item"
    assert all(ln.get("source") != beo_proposals.LINE_SOURCE_PROPOSAL for ln in wizard_lines), (
        "a wizard line must never carry the sync's ownership mark"
    )
    wizard_invoice = invoicing.create_final_invoice(
        db, booking, line_items=wizard_lines,
        due_date=policy.final_balance_due_date(booking.event_date, issued_on=dt.date.today()), actor="wizard_client:test",
    )
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    db.refresh(wizard_invoice)
    assert [(ln["description"], ln["quantity"]) for ln in wizard_invoice.line_items] == [("Grazing Platter", 2)],         "one platter line, not two"
    assert str(wizard_invoice.total) == "500.00"


def test_a_sent_final_invoice_is_left_alone_and_the_booking_page_says_so(admin_client, db, loft, menu_items):
    from app.services import invoicing

    booking = _confirmed(db, _booking(db, loft))
    sent = invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Original", "quantity": 1, "unit_price": "10.00"}],
        due_date=dt.date(2027, 5, 7), actor="staff:aaron",
    )
    invoicing.mark_sent(db, sent, actor="staff:aaron")
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    db.refresh(sent)
    assert [ln["description"] for ln in sent.line_items] == ["Original"] and sent.status.value == "sent"
    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter", "the Event Order still took the lines"
    outcome = _invoice_events(db, booking)[-1].new_value
    assert f"{sent.invoice_reference} is already sent and was left alone" in outcome
    page = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "did not reach the final invoice" in page and "left alone" in page


def test_an_undated_booking_gets_the_lines_but_no_invoice(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    booking.event_date = None
    db.commit()

    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert _final_invoices(db, booking) == []
    assert "no event date" in _invoice_events(db, booking)[-1].new_value


def test_an_invoice_failure_never_undoes_the_approved_lines(db, loft, menu_items, monkeypatch):
    from app.services import invoicing

    def refuse(*args, **kwargs):
        raise ValueError("simulated: invoicing refused")

    monkeypatch.setattr(invoicing, "create_final_invoice", refuse)
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    db.refresh(document)
    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter"
    assert _final_invoices(db, booking) == []
    assert "not updated: simulated" in _invoice_events(db, booking)[-1].new_value
    assert beo_proposals.latest_food_invoice_notice(booking).startswith("final invoice not updated")


def test_a_built_draft_invoice_needs_no_notice(db, loft, menu_items):
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    db.refresh(booking)

    assert beo_proposals.latest_food_invoice_notice(booking) is None, "a draft in the invoice list is its own notice"


def test_the_deposit_credit_is_re_derived_when_the_invoice_goes_out(db, loft, menu_items):
    """Approval now routinely precedes the deposit payment, so a draft
    built before it carried no credit and would have billed the deposit
    twice. The credit is derived, never typed -- so it is derived once
    more at the moment the invoice becomes a claim on a client."""
    from app.models import BookingEvent as _Event
    from app.services import invoicing
    from tests.test_wizard_generation import _pay_deposit

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    invoice = _final_invoices(db, booking)[0]
    assert str(invoice.total) == "500.00" and len(invoice.line_items) == 1, "no deposit paid yet"

    _pay_deposit(db, booking)  # the client pays AFTER the draft was built
    invoicing.mark_sent(db, invoice, actor="staff:aaron")

    db.refresh(invoice)
    assert [(ln["description"], ln["unit_price"]) for ln in invoice.line_items] == [
        ("Grazing Platter", "250.00"), ("Less: deposit credited", "-500.00")
    ]
    assert str(invoice.total) == "0.00", "the client is not billed the deposit twice"
    kinds = [e.event_type for e in db.query(_Event).filter_by(booking_id=booking.id).all()]
    assert "invoice_credit_rederived" in kinds


def test_sending_an_already_correct_invoice_records_no_correction(db, loft, menu_items):
    from app.models import BookingEvent as _Event
    from app.services import invoicing
    from tests.test_wizard_generation import _pay_deposit

    booking = _confirmed(db, _booking(db, loft))
    _pay_deposit(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 3))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    invoice = _final_invoices(db, booking)[0]
    before = str(invoice.total)

    invoicing.mark_sent(db, invoice, actor="staff:aaron")

    db.refresh(invoice)
    assert str(invoice.total) == before == "250.00"
    assert "invoice_credit_rederived" not in [e.event_type for e in db.query(_Event).filter_by(booking_id=booking.id).all()]


def test_no_invoice_is_built_for_a_cancelled_booking(db, loft, menu_items):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    change_status(db, booking, BookingStatus.cancelled, actor="staff:aaron")

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert document.content["food_order"]["line_items"], "the run sheet still took the lines"
    assert _final_invoices(db, booking) == []
    assert "this booking is cancelled" in _invoice_events(db, booking)[-1].new_value


def test_the_banner_retires_once_somebody_has_dealt_with_it(admin_client, db, loft, menu_items):
    """A banner that never clears is one staff learn to scroll past."""
    from app.services import invoicing

    booking = _confirmed(db, _booking(db, loft))
    sent = invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Original", "quantity": 1, "unit_price": "10.00"}],
        due_date=dt.date(2027, 5, 7), actor="staff:aaron",
    )
    invoicing.mark_sent(db, sent, actor="staff:aaron")
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )
    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")
    db.refresh(booking)
    assert beo_proposals.latest_food_invoice_notice(booking), "raised"
    assert "did not reach the final invoice" in admin_client.get(f"/admin/bookings/{booking.id}").text

    invoicing.cancel_invoice(db, sent, actor="staff:aaron")  # the hand revise it asked for
    db.refresh(booking)

    assert beo_proposals.latest_food_invoice_notice(booking) is None, "it retires once somebody acts"
    assert "did not reach the final invoice" not in admin_client.get(f"/admin/bookings/{booking.id}").text


def test_a_failure_inside_the_sync_is_a_trail_row_not_a_five_hundred(db, loft, menu_items, monkeypatch):
    """Only ValueError used to be caught, so a database-level failure was
    a bare 500 with nothing recorded -- and my own NameError in the first
    cut of this fix was caught by the widened handler."""
    from app.services import invoicing

    def explode(*args, **kwargs):
        raise RuntimeError("simulated: the database went away")

    monkeypatch.setattr(invoicing, "create_final_invoice", explode)
    booking = _confirmed(db, _booking(db, loft))
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    document = beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    assert document.content["food_order"]["line_items"][0]["description"] == "Grazing Platter"
    assert "the database went away" in _invoice_events(db, booking)[-1].new_value


def test_an_empty_draft_invoice_is_filled_rather_than_left_alone(db, loft, menu_items):
    """There is nothing on it to lose, and a banner asking somebody to
    reconcile an invoice with no lines on it helps nobody."""
    from app.services import invoicing

    booking = _confirmed(db, _booking(db, loft))
    empty = invoicing.create_final_invoice(
        db, booking, line_items=[], due_date=dt.date(2027, 5, 7), actor="staff:aaron"
    )
    assert empty.line_items == []
    proposal, _ = beo_proposals.propose(
        db, booking, fields={}, source="email", actor="ai:claude", food_order=_food(menu_items, ("Grazing Platter", 2))
    )

    beo_proposals.approve_field(db, _food_row(proposal), actor="staff:aaron")

    db.refresh(empty)
    assert [(ln["description"], ln["quantity"]) for ln in empty.line_items] == [("Grazing Platter", 2)]
    assert "refreshed" in _invoice_events(db, booking)[-1].new_value
    assert beo_proposals.latest_food_invoice_notice(booking) is None


def test_sending_a_deposit_invoice_never_credits_it_against_itself(db, loft):
    """The re-derive is a FINAL-invoice rule. A deposit invoice crediting
    the deposit against itself would halve what the client is asked for."""
    from decimal import Decimal

    from app.models.invoice import InvoiceType
    from app.models.payment import PaymentMethod
    from app.services import invoicing

    booking = _confirmed(db, _booking(db, loft))
    first = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date(2027, 1, 1), actor="staff:aaron",
    )
    invoicing.mark_sent(db, first, actor="staff:aaron")
    invoicing.record_payment(db, first, amount=Decimal("500.00"), method=PaymentMethod.card, actor="staff:aaron")
    second = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Second deposit", "quantity": 1, "unit_price": "300.00"}],
        dt.date(2027, 1, 1), actor="staff:aaron",
    )

    invoicing.mark_sent(db, second, actor="staff:aaron")

    db.refresh(second)
    assert [ln["description"] for ln in second.line_items] == ["Second deposit"], "no credit line on a deposit invoice"
    assert str(second.total) == "300.00"
