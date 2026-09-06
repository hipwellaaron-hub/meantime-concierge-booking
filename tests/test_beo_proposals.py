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
from app.services import document_generation
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


def _beo(db, booking, **content_overrides):
    """An Event Order whose overridden fields read as HUMAN-written.

    Every real route to such a value -- a hand-edit through
    documents.update_content, or an approval through update_content_fields
    -- takes the key out of the generator's derived set. A test that
    overrode the content dict directly skipped that, so the regenerate
    guard correctly saw the generator's own output and had nothing to
    protect. Marking it here keeps the fixture honest about what it is
    standing in for (2026-09-07 review).
    """
    content = generate_beo_content(booking)
    content.update(content_overrides)
    document_generation.mark_authored(content, list(content_overrides))
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
def test_decoration_supplier_and_setup_language_is_flagged_in_dietaries(dietaries):
    """The error that prompted the feature: a decoration note transcribed
    into Dietaries, taking a declared allergy down with it.

    Flagged, not blocked. A blocked proposal is stored for calibration and
    never shown to staff, so blocking a dietary value means the declared
    allergy reaches nobody -- and this pattern fires on ordinary allergy
    notes ("GF cake delivered by client"). Aaron's RSA ruling, in the field
    where it matters most: a messy allergy note reaches the kitchen, a
    blocked one does not exist."""
    result = beo_rules.validate({"dietaries": dietaries})
    assert beo_rules.DIETARY_CONTAMINATION in result.warning_codes
    assert not result.blocked


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
    """Uses CLIENT_PROSE: the dietary rules deliberately warn rather than
    block now, precisely so a dietary value is never the thing withheld."""
    booking = _booking(db, loft)
    _beo(db, booking)

    resp = _propose(ai_client, booking, {"special_notes": "We have organised a cake"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert beo_rules.CLIENT_PROSE in detail["rule_codes"]
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
    assert beo_rules.DROPS_DIETARY in result.warning_codes
    assert "nut" in result.warning_note()
    # Warned, never blocked: a withheld allergy is the worse failure.
    assert not result.blocked
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
            "value_dietaries": "I will bring the balloon arch myself",
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

    other = Venue(name="Meantime The Entrance", slug="entrance")
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
def test_the_wider_decoration_vocabulary_is_flagged_in_dietaries(dietaries):
    result = beo_rules.validate({"dietaries": dietaries})
    assert beo_rules.DIETARY_CONTAMINATION in result.warning_codes
    assert not result.blocked, "a dietary value is never the thing withheld"


def test_invisible_characters_do_not_smuggle_a_word_past_the_dietary_rule():
    result = beo_rules.validate({"dietaries": "ball\u200doons, no nuts"})
    assert beo_rules.DIETARY_CONTAMINATION in result.warning_codes


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
    uses, which requires birthday context rather than the digits alone."""
    assert not beo_rules.validate(
        {"special_notes": "Rounds of 8."}, event_type="corporate", event_name="Team lunch 18 Nov", child_count=4
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


def test_the_generators_own_output_is_never_a_loss(db, loft):
    """The question is no longer "does this text look generated?" -- which
    was unanswerable the moment the generator composed a value instead of
    emitting a constant, and is what reported stale contract terms as a
    human's words. document_generation stamps what it derived; nothing in
    an untouched document is at risk."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Untouched")
    doc = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    assert dr.authored_keys(db, doc) == set()
    assert dr.losses(db, doc, generate_beo_content(booking)) == []


def test_a_composed_generator_value_is_not_mistaken_for_a_human_value(db, loft):
    """The case the old text test got wrong: a bar credit makes the
    generator's own bar_structure a composed string that matches no fixed
    placeholder, so it read as "written by a person" and a regenerate
    offered to keep a superseded credit figure."""
    from decimal import Decimal

    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Composed Value")
    booking.bar_credit = Decimal("250")
    db.flush()
    doc = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    assert "250" in doc.content["bar_structure"]

    booking.bar_credit = Decimal("500")
    db.flush()
    fresh = generate_beo_content(booking)
    assert "500" in fresh["bar_structure"]
    assert [loss.field for loss in dr.losses(db, doc, fresh)] == [], "the generator's own value is its own"


def test_a_staff_note_that_mentions_the_review_marker_is_still_protected(db, loft):
    """Staff reuse the [REVIEW] convention in their own notes. Under the old
    text test one of these was classed as a placeholder and regenerated over
    without asking; authorship does not care what the words look like."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Marker Note")
    doc = _beo(db, booking, special_notes="[REVIEW] with Aaron: read the nut allergy back to the kitchen")
    fresh = generate_beo_content(booking)
    assert [loss.field for loss in dr.losses(db, doc, fresh)] == ["special_notes"]


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
            shown = dr.losses(first, documents_service.get_current(first, booking_id, DocumentType.beo), fresh)
            expect = dr.fingerprint(shown)
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
            assert dr.fingerprint(now) != expect, "the stale decision must be refused"
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


def test_an_emptied_box_is_a_blank_not_a_licence_to_write_the_ai_text(admin_client, db, loft):
    """FastAPI turns an empty form value into the field's default, so ""
    and "absent" both arrived as None -- and None meant "keep what was
    proposed". So a staff member who deleted the whole box and clicked
    Approve got the AI's original text written, recorded as approved
    unedited (2026-09-07 review). The form now says which boxes it
    rendered, so an emptied one is a real blank and the house rules refuse
    it, which is what the route's own comment always claimed."""
    booking = _booking(db, loft, name="Emptied Box")
    document = _beo(db, booking, dietaries="1x severe nut allergy (table 4).")
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "Balloons behind cake table, 1x nut allergy"},
        source="email", actor="ai:claude",
    )
    row = proposal.fields[0]
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    assert 'name="submitted_fields" value="dietaries"' in page, "the form must declare its boxes"

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/beo-proposals/{proposal.id}/review",
        data={
            "csrf_token": csrf, "action": f"approve:{row.id}",
            "submitted_fields": "dietaries", "value_dietaries": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 409, "emptying a field that holds a declared allergy is refused"
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["dietaries"] == "1x severe nut allergy (table 4).", "nothing was written"
    row = db.get(BeoProposalField, row.id)
    assert row.state == FIELD_PENDING and row.applied_value is None


def test_a_hand_edit_cannot_revert_an_approval_that_landed_while_it_was_open(admin_client, db, loft):
    """The form posts a whole content dict built when the page rendered, so
    it silently reverted anything written in between -- an approval applying
    a declared allergy to a field the editor never touched. The row lock did
    not help: by then the stale dict was already in hand (2026-09-07)."""
    booking = _booking(db, loft, name="Stale Edit")
    document = _beo(db, booking)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    expect = re.search(r'name="content_expect" value="([^"]+)"', page).group(1)

    # Meanwhile, an approval writes the allergy onto the same document.
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x severe nut allergy (table 4)."},
        source="client email", actor="ai:claude",
    )
    beo_proposals.approve_field(db, proposal.fields[0], actor="staff:liz")
    db.expire_all()

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            "csrf_token": csrf, "content_expect": expect,
            "catering_order_and_service_style": "Grazing on arrival.",
            "bar_structure": "", "room_layout_notes": "", "music": "", "entertainment": "",
            "dietaries": "", "accessibility": "", "decorations": "", "special_notes": "",
            "av_notes": "", "internal_notes": "typo fixed", "status_text": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 409, "a stale save must be refused, not applied"
    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["dietaries"] == "1x severe nut allergy (table 4).", "the approval stands"


def test_the_calibration_read_survives_the_proposal_resolving(ai_client, db, loft):
    """Once every field is decided the proposal RESOLVES -- which is exactly
    when applied_value next to proposed_value becomes worth reading. A
    pending-only read went null at that moment, so the model was told to
    read the correction before proposing and could never see it, and
    re-proposed wording a human had already rewritten (2026-09-07 review)."""
    booking = _booking(db, loft, name="Calibration")
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "1x nut allergy"}, source="client email", actor="ai:claude"
    )
    # Aaron rewrites it before approving -- the signal itself.
    beo_proposals.approve_field(
        db, proposal.fields[0], actor="staff:aaron", value="1x severe nut allergy (table 4)."
    )
    db.expire_all()
    assert beo_proposals.pending_proposal(db, booking.id) is None, "nothing is pending any more"

    resp = ai_client.get(f"/api/ai/bookings/{booking.reference_code}/event-order-proposal")

    assert resp.status_code == 200
    body = resp.json()["proposal"]
    assert body is not None, "the decided proposal must still be readable"
    field = next(f for f in body["fields"] if f["field"] == "dietaries")
    assert field["proposed_value"] == "1x nut allergy"
    assert field["applied_value"] == "1x severe nut allergy (table 4)."
    assert field["edited_before_approval"] is True


def test_approving_a_bar_structure_keeps_the_bar_credit_promise(db, loft):
    """The credit line is generated from booking.bar_credit and printed only
    inside bar_structure. A proposal replaces the whole field, so approving
    one deleted the promise the floor has to honour -- with no rule covering
    it, unlike the music equivalent (2026-09-07 review)."""
    from decimal import Decimal

    booking = _booking(db, loft, name="Bar Credit")
    booking.bar_credit = Decimal("250")
    db.flush()
    _beo(db, booking)

    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": "Tab to $1,500, then cash bar."},
        source="client email", actor="ai:claude",
    )
    assert not result.blocked, result.codes
    beo_proposals.approve_field(db, proposal.fields[0], actor="staff:aaron")

    db.expire_all()
    written = documents_service.get_current(db, booking.id, DocumentType.beo).content["bar_structure"]
    assert "Tab to $1,500, then cash bar." in written, "the approved wording is there"
    assert "250" in written and "bar credit" in written.lower(), "and so is the credit the floor must honour"


def test_no_bar_credit_means_no_invented_credit_line(db, loft):
    booking = _booking(db, loft, name="No Bar Credit")
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking, fields={"bar_structure": "Cash bar all night."},
        source="client email", actor="ai:claude",
    )
    beo_proposals.approve_field(db, proposal.fields[0], actor="staff:aaron")
    db.expire_all()
    written = documents_service.get_current(db, booking.id, DocumentType.beo).content["bar_structure"]
    assert written == "Cash bar all night."


# --- documents written before authorship was recorded -------------------------


def test_a_pre_provenance_document_nobody_touched_regenerates_freely(db, loft):
    """No stamp and no human write recorded against the version: the
    generator wrote all of it, which is exact rather than a guess. This is
    every document that existed before the redesign."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Legacy Untouched")
    content = generate_beo_content(booking)
    content.pop("_derived")  # as an older document has it
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")

    assert dr.authored_keys(db, doc) == set()
    assert dr.losses(db, doc, generate_beo_content(booking)) == []


def test_a_pre_provenance_document_a_human_touched_protects_everything(db, loft):
    """A hand-edit is recorded per VERSION, not per field, so which key is
    unknowable. Every differing field is treated as at risk -- over-keeping
    is recoverable on the screen, silent loss is not."""
    from app.services import document_regeneration as dr

    booking = _booking(db, loft, name="Legacy Edited")
    content = generate_beo_content(booking)
    content.pop("_derived")
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")
    edited = dict(doc.content)
    edited["room_layout_notes"] = "Rounds of 8, dance floor centre."
    documents_service.update_content(db, doc, edited, actor="staff:aaron")
    db.expire_all()
    doc = documents_service.get_current(db, booking.id, DocumentType.beo)

    assert dr.authored_keys(db, doc) is None, "unknowable, and says so"
    fields = [loss.field for loss in dr.losses(db, doc, generate_beo_content(booking))]
    assert "room_layout_notes" in fields


def test_approving_a_bar_structure_that_repeats_the_credit_line_does_not_double_it(db, loft):
    """The review panel shows staff the current value, credit line and all,
    so the text coming back may already carry it. Re-composing blindly
    printed the promise twice (found reviewing the fix, 2026-09-07)."""
    from decimal import Decimal

    booking = _booking(db, loft, name="Bar Credit Echo")
    booking.bar_credit = Decimal("250")
    db.flush()
    _beo(db, booking)
    echoed = "$250 bar credit included, applied on the night.\n\nTab to $1,500, then cash bar."

    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": echoed}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    beo_proposals.approve_field(db, proposal.fields[0], actor="staff:aaron")

    db.expire_all()
    written = documents_service.get_current(db, booking.id, DocumentType.beo).content["bar_structure"]
    assert written.count("bar credit included") == 1, written
