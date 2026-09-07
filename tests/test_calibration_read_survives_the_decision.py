"""The calibration signal must be readable once it exists.

The point of the Event Order proposal read, in the MCP tool's own words,
is that "for anything already decided it shows `applied_value` next to
`proposed_value`, so you can see where a human rewrote a transcription
before approving it. That difference is the calibration signal -- read it
and write closer to what they actually wanted."

applied_value and edited_before_approval are written as each field is
decided, and deciding the LAST one resolves the proposal. The endpoint
read the latest PENDING proposal, so the whole object went null at exactly
the moment the signal became complete: propose two fields, have a human
edit one and approve both, and the read that was supposed to teach from
that returns `proposal: null`.

This repair existed on 2bbd23e and went away with the revert of that
commit. It is being re-done deliberately rather than rediscovered.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Contact
from app.models.beo_proposal import BeoProposalField
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

# The same credential the other AI-endpoint tests use.
TOKEN = "test-ai-token-do-not-use-in-production"

TRANSCRIBED = "1x nut allergy, table 4"
AS_APPROVED = "1x severe nut allergy (table 4). Kitchen briefed."


def _booking(db, space, name="Calibration"):
    contact = Contact(name="Calibration Client", email=f"cal.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _beo(db, booking, **overrides):
    content = generate_beo_content(booking)
    content.update(overrides)
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")


def _read(client, booking):
    resp = client.get(f"/api/ai/bookings/{booking.reference_code}/event-order-proposal")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _field(payload, name):
    return next(row for row in payload["proposal"]["fields"] if row["field"] == name)


@pytest.fixture()
def ai_client(db, hamilton, monkeypatch):
    """Same shape as the AI client in tests/test_beo_proposals.py: the read
    gate open, writes enabled, and get_db pointed at this test's session."""
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


@pytest.fixture()
def decided(ai_client, db, loft):
    """A proposal a human has finished with: one field edited before
    approval, one taken as proposed. Nothing is left pending, so the
    proposal is resolved -- which is precisely when the signal exists."""
    booking = _booking(db, loft)
    _beo(db, booking)
    proposal, _ = beo_proposals.propose(
        db, booking,
        fields={"dietaries": TRANSCRIBED, "room_layout_notes": "Long tables, 3 rows."},
        source="client email", actor="ai:claude",
    )
    dietaries = db.get(BeoProposalField, next(f.id for f in proposal.fields if f.field == "dietaries"))
    beo_proposals.approve_field(db, dietaries, actor="staff:liz", value=AS_APPROVED)
    layout = db.get(BeoProposalField, next(f.id for f in proposal.fields if f.field == "room_layout_notes"))
    beo_proposals.approve_field(db, layout, actor="staff:liz")
    db.flush()
    return booking, proposal


def test_the_read_still_answers_once_every_field_is_decided(decided, ai_client, db):
    booking, proposal = decided
    assert beo_proposals.pending_proposal(db, booking.id) is None, "nothing is outstanding"

    payload = _read(ai_client, booking)

    assert payload["proposal"] is not None, "the answer vanished at the moment it existed"
    assert payload["proposal"]["proposal_id"] == str(proposal.id)


def test_it_shows_where_the_human_rewrote_the_transcription(decided, ai_client):
    booking, _ = decided
    payload = _read(ai_client, booking)

    row = _field(payload, "dietaries")
    assert row["proposed_value"] == TRANSCRIBED
    assert row["applied_value"] == AS_APPROVED
    assert row["edited_before_approval"] is True

    untouched = _field(payload, "room_layout_notes")
    assert untouched["edited_before_approval"] is False
    assert untouched["applied_value"] == untouched["proposed_value"]


def test_status_is_what_says_whether_anybody_still_has_to_look(decided, ai_client):
    """The contract change: a non-null proposal no longer means pending.
    The discriminator has to be in the payload, or a caller reads a
    finished ask as outstanding work."""
    booking, _ = decided
    payload = _read(ai_client, booking)

    assert payload["proposal"]["status"] == "resolved"
    assert all(row["state"] == "approved" for row in payload["proposal"]["fields"])


def test_a_pending_proposal_still_reads_as_pending(ai_client, db, loft):
    """The half that already worked, pinned so widening the read does not
    quietly change what an outstanding ask looks like."""
    booking = _booking(db, loft, "Still Pending")
    _beo(db, booking)
    beo_proposals.propose(
        db, booking, fields={"dietaries": TRANSCRIBED}, source="client email", actor="ai:claude"
    )
    db.flush()

    payload = _read(ai_client, booking)

    assert payload["proposal"]["status"] == "pending"
    row = _field(payload, "dietaries")
    assert row["state"] == "pending"
    assert row["applied_value"] is None


def test_nothing_ever_proposed_is_the_only_null(ai_client, db, loft):
    booking = _booking(db, loft, "Never Proposed")
    _beo(db, booking)

    assert _read(ai_client, booking)["proposal"] is None


def test_a_newer_ask_is_what_the_read_returns(ai_client, db, loft):
    """One proposal, not a history -- the read exists to be taken right
    before the next propose.

    Both are created in one transaction, so created_at is identical to the
    microsecond (`now()` is transaction time). Superseding is what orders
    them, which is exactly the tie-break latest_proposal documents.
    """
    booking = _booking(db, loft, "Superseding")
    _beo(db, booking)
    beo_proposals.propose(
        db, booking, fields={"dietaries": TRANSCRIBED}, source="first email", actor="ai:claude"
    )
    second, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": "2x coeliac."}, source="second email", actor="ai:claude"
    )
    db.flush()

    payload = _read(ai_client, booking)

    assert payload["proposal"]["proposal_id"] == str(second.id)
    assert _field(payload, "dietaries")["proposed_value"] == "2x coeliac."


def test_a_new_ask_hides_the_signal_from_the_finished_one(ai_client, db, loft):
    """The documented limit, pinned rather than left to be discovered. The
    read returns one proposal, so once a fresh ask is outstanding the
    applied_value from the last decided one is no longer visible here --
    which is why the tool description calls this the read before the next
    propose and not a record of every correction.

    Stamped timestamps, because these two are minutes apart in production
    and identical inside one test transaction; the earlier one is resolved
    rather than pending, so nothing supersedes it.
    """
    booking = _booking(db, loft, "Hidden Signal")
    _beo(db, booking)
    finished, _ = beo_proposals.propose(
        db, booking, fields={"dietaries": TRANSCRIBED}, source="first email", actor="ai:claude"
    )
    row = db.get(BeoProposalField, finished.fields[0].id)
    beo_proposals.approve_field(db, row, actor="staff:liz", value=AS_APPROVED)
    fresh, _ = beo_proposals.propose(
        db, booking, fields={"room_layout_notes": "Long tables, 3 rows."},
        source="second email", actor="ai:claude",
    )
    db.flush()
    finished.created_at = dt.datetime(2027, 5, 1, 9, 0, tzinfo=dt.timezone.utc)
    fresh.created_at = dt.datetime(2027, 5, 1, 9, 30, tzinfo=dt.timezone.utc)
    db.flush()

    payload = _read(ai_client, booking)

    assert payload["proposal"]["proposal_id"] == str(fresh.id)
    assert payload["proposal"]["status"] == "pending"
    assert [row["field"] for row in payload["proposal"]["fields"]] == ["room_layout_notes"]

def test_the_tool_description_no_longer_promises_null_means_nothing_outstanding():
    """The words the model is given have to match what it will receive: a
    resolved proposal comes back as an object now, and a description
    saying null means "nothing outstanding" would teach it to read a
    finished ask as pending work."""
    from mcp_server.tools import TOOLS

    description = next(t["description"] for t in TOOLS if t["name"] == "event_order_proposal")
    assert "Returns `proposal: null` when nothing is outstanding." not in description
    assert "nothing has ever been proposed" in description
    assert "MOST RECENT" in description
