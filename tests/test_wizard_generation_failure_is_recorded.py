"""A client whose answers are saved and whose documents are not.

wizard.submit_review commits the session as `submitted` and THEN builds the
Event Order and invoice. Anything that raises in between leaves the client
finished, the booking without documents, and _lock_and_guard_editable
refusing every retry with "already submitted". Nothing recorded it: the
audit trail showed wizard_submitted and stopped.

That window is not new -- it has been open since the commit moved above the
generation call, and a broken create_invoice strands a client exactly the
same way. What ea92c67 changed was the symptom: it added a RuntimeError to
documents.lock_current_for_update, and an unhandled RuntimeError reaches the
client as a text/plain 500, which makes the wizard's own fetch helper throw
a JSON parse error -- so the alert() the client sees says nothing at all.

So: the failure is written to the audit trail in its own transaction, and
the route answers 503 with a detail the client can actually read. Nothing
is generated and nothing is overwritten -- the raise this started from
exists to stop a regenerate writing over a person's words, and that is
still exactly what it does.

WHAT THESE TESTS DO NOT COVER: the retry loop in lock_current_for_update.
Raising from a monkeypatched lock proves the strand and proves nothing
about exhaustion, which needs five concurrent supersedes and real threads
(tests/test_locked_read_survives_a_race.py is where that lives).
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
from app.main import app
from app.models import BookingEvent, Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services import wizard as wizard_service
from app.services import wizard_generation
from app.services.booking import change_status, create_booking
from app.services.document_generation import generate_beo_content

from tests.test_wizard_generation import _complete_all_steps

ALLERGY = "1x severe nut allergy (table 4)."


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _booking_with_a_current_event_order(db, loft):
    """A booking that ALREADY has a current Event Order.

    Load-bearing setup: with no current document lock_current_for_update
    returns None and never raises at all, so the same test on a fresh
    booking would pass without exercising anything.
    """
    contact = Contact(name="Strand Client", email=f"strand.{dt.datetime.now().timestamp()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 3, 6),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Strand Test",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    content = generate_beo_content(booking)
    content["dietaries"] = ALLERGY
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")
    return booking


def _ready_to_submit(db, loft, menu_items):
    booking = _booking_with_a_current_event_order(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)
    return booking, session


def _failures(db, booking):
    return db.query(BookingEvent).filter_by(
        booking_id=booking.id, event_type="wizard_generation_failed"
    ).all()


def _break_generation(monkeypatch, exc):
    """Stand in for a generation that cannot finish. The real trigger this
    was found through is lock_current_for_update exhausting its retries;
    what matters here is what happens to the client and the record when
    ANYTHING in that window raises."""
    def boom(*args, **kwargs):
        raise exc
    monkeypatch.setattr(wizard_generation.documents_service, "lock_current_for_update", boom)


def test_the_client_is_told_something_they_can_read(db, loft, menu_items, client, monkeypatch):
    """Not a text/plain 500. The wizard's fetch helper parses JSON and
    alert()s the detail; without one the client is shown nothing."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    _break_generation(monkeypatch, RuntimeError("could not lock the current beo"))

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 503, response.status_code
    detail = response.json()["detail"]
    assert "answers are saved" in detail
    assert "do not need to submit again" in detail.lower()


def test_the_failure_is_written_down(db, loft, menu_items, client, monkeypatch):
    """The whole point. Before this the audit trail showed wizard_submitted
    and stopped, so nothing anywhere said the client got no Event Order."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    _break_generation(monkeypatch, RuntimeError("could not lock the current beo"))

    client.post(f"/w/{session.access_token}/review", json={})

    recorded = _failures(db, booking)
    assert len(recorded) == 1, recorded
    assert "RuntimeError" in recorded[0].new_value
    assert "could not lock the current beo" in recorded[0].new_value


def test_nothing_was_generated_and_nothing_was_overwritten(db, loft, menu_items, client, monkeypatch):
    """The raise exists to stop a rebuild writing over a person's words.
    Reporting the failure must not quietly let it through."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    _break_generation(monkeypatch, RuntimeError("could not lock the current beo"))

    client.post(f"/w/{session.access_token}/review", json={})

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.version == 1, "a version was written despite the failure"
    assert current.content["dietaries"] == ALLERGY


def test_the_answers_really_are_saved(db, loft, menu_items, client, monkeypatch):
    """The message tells the client their answers are saved, so that had
    better be true -- the session is committed as submitted before
    generation runs, which is exactly why the strand exists."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    _break_generation(monkeypatch, RuntimeError("could not lock the current beo"))

    client.post(f"/w/{session.access_token}/review", json={})

    db.refresh(session)
    assert session.status.value == "submitted"
    assert session.submitted_at is not None


def test_any_failure_in_that_window_is_recorded_not_only_the_lock(db, loft, menu_items, client, monkeypatch):
    """The window is wider than the RuntimeError that led here: a
    ValueError from anywhere in generation stranded a client the same way,
    and reached them as a 422 quoting an internal message."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    _break_generation(monkeypatch, ValueError("cannot create a document on a linked booking"))

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 503, "not a 422 quoting internals at the client"
    assert "linked booking" not in response.json()["detail"]
    recorded = _failures(db, booking)
    assert len(recorded) == 1
    assert "ValueError" in recorded[0].new_value, "the internal message is kept where staff read it"


def test_a_second_submit_is_still_the_ordinary_already_submitted_answer(db, loft, menu_items, client, monkeypatch):
    """The double-submit guard raises a ValueError that means the OPPOSITE
    of a failure -- the client already succeeded. It must stay a 409 and
    must not be recorded as a generation failure."""
    booking, session = _ready_to_submit(db, loft, menu_items)
    token = session.access_token
    first = client.post(f"/w/{token}/review", json={})
    assert first.status_code == 200, first.text

    second = client.post(f"/w/{token}/review", json={})

    assert second.status_code == 409, second.text
    assert wizard_service.ALREADY_SUBMITTED_MESSAGE in second.json()["detail"]
    assert _failures(db, booking) == [], "a successful submission recorded a failure"


def test_a_submission_that_works_records_no_failure(db, loft, menu_items, client):
    booking, session = _ready_to_submit(db, loft, menu_items)

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 200, response.text
    assert _failures(db, booking) == []
    db.expire_all()
    assert documents_service.get_current(db, booking.id, DocumentType.beo).version == 2


def test_a_database_error_is_recorded_too(db, loft, menu_items, client, monkeypatch):
    """The case the rollback exists for, and the only one that can prove it.

    Every fault above is raised from Python, which leaves the transaction
    perfectly usable -- so the recorder would write its event with or
    without the rollback (mutation-checked: removing it survives all of
    them). A failed STATEMENT is different: Postgres puts the transaction
    in an aborted state and every later statement raises PendingRollback
    until somebody rolls back. That is the shape a real generation failure
    takes when the database is what broke, and without the rollback the
    failure would go unrecorded for exactly the reason it most needed
    recording.
    """
    booking, session = _ready_to_submit(db, loft, menu_items)

    def broken_statement(dbsession, *args, **kwargs):
        dbsession.execute(text("SELECT * FROM a_table_that_does_not_exist"))

    monkeypatch.setattr(
        wizard_generation.documents_service, "lock_current_for_update", broken_statement
    )

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 503, response.status_code
    recorded = _failures(db, booking)
    assert len(recorded) == 1, "the database error went unrecorded"
    assert "a_table_that_does_not_exist" in recorded[0].new_value
