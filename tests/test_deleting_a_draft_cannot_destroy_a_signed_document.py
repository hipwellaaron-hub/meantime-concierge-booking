"""Delete cannot remove a document that stopped being a draft.

THE LOSS, and it is a signed contract. The booking page renders a Delete
button against a draft. The route then loads that document with an
UNLOCKED `db.get` (admin_bookings.py) into a session with
expire_on_commit=False, and delete_draft checked `document.status` off
that Python attribute -- a snapshot of the row as it was when the page was
rendered, minutes earlier. A client signing, or another staff member
pressing Send, in between was invisible: the stale `draft` passed the only
guard, and the DELETE matched on id alone. Signature, signer name,
signed_at, the version the client is holding: gone, with a 303 and a
success banner.

Every sibling writer in documents.py re-reads under a lock immediately
above its status check. This one never did, and its own comment explains
why it takes no lock: the module has an unsettled lock-ordering
disagreement, and joining either camp was shown to deadlock.

SO THE DELETE ITSELF DECIDES. `DELETE ... WHERE id = :id AND status =
'draft'`, and rowcount is the answer. That takes exactly the row lock the
DELETE already took, at exactly the point it already took it -- the lock
graph is unchanged, which matters because three independent attempts to
settle the ordering were each shown to open a cycle somewhere else. And it
is STRICTLY stronger than a locked re-read: there is no window at all
between the check and the write, because they are one statement.

The audit event is written only after it succeeds. booking_events is
append-only by trigger, so a refusal that had already inserted
"document_deleted" would leave a permanent audit row for a deletion that
never happened.
"""
import datetime as dt
import threading
import uuid

import pytest
from sqlalchemy import select

from app.models import BookingEvent, Contact, Space, Venue
from app.models.document import Document, DocumentStatus, DocumentType
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content
from app.services.documents import (
    create_new_version,
    delete_draft,
    get_current,
    mark_sent,
    sign,
)
from tests.conftest import TestSessionLocal, purge_venue


# --- the single-session case: a stale attribute ------------------------


def _draft(db, booking):
    return create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )


def test_a_draft_is_still_deletable(db, booking):
    """The positive control, and it earns its place: a conditional DELETE
    that matched nothing would refuse everything and pass every probe
    below."""
    document = _draft(db, booking)

    delete_draft(db, document, actor="staff:test")

    assert db.get(Document, document.id) is None


def test_a_document_signed_since_the_page_loaded_is_not_deleted(db, booking):
    """THE one. The in-memory object still says draft -- that is the whole
    point -- and the row does not."""
    document = _draft(db, booking)
    mark_sent(db, document, actor="staff:test")
    sign(db, document, signer_name="A Client", signer_ip="1.2.3.4")

    # Put the object back into the state the route's unlocked db.get would
    # have handed us: the row moved on, this copy did not. Setting the
    # attribute directly is exactly the staleness being simulated -- it is
    # not a shortcut around the guard, because the guard is in the SQL.
    db.expire(document)
    assert document.status is DocumentStatus.signed
    document.__dict__["status"] = DocumentStatus.draft

    with pytest.raises(ValueError) as exc:
        delete_draft(db, document, actor="staff:test")

    assert "signed" in str(exc.value)
    survivor = db.get(Document, document.id)
    assert survivor is not None, "a SIGNED agreement was deleted"
    assert survivor.signer_name == "A Client"
    assert survivor.signed_at is not None


def test_a_document_sent_since_the_page_loaded_is_not_deleted(db, booking):
    document = _draft(db, booking)
    mark_sent(db, document, actor="staff:test")

    db.expire(document)
    document.__dict__["status"] = DocumentStatus.draft

    with pytest.raises(ValueError):
        delete_draft(db, document, actor="staff:test")

    assert db.get(Document, document.id) is not None


def test_a_refusal_writes_no_audit_row(db, booking):
    """booking_events is append-only by database trigger. A refusal that
    had already inserted "document_deleted" would leave a permanent record
    of a deletion that never happened, and nothing could remove it."""
    document = _draft(db, booking)
    mark_sent(db, document, actor="staff:test")

    db.expire(document)
    document.__dict__["status"] = DocumentStatus.draft

    with pytest.raises(ValueError):
        delete_draft(db, document, actor="staff:test")

    events = db.scalars(
        select(BookingEvent).where(
            BookingEvent.booking_id == booking.id,
            BookingEvent.event_type == "document_deleted",
        )
    ).all()
    assert events == [], "a refused delete recorded itself as having happened"


def test_a_successful_delete_still_records_itself(db, booking):
    """The other half. Moving the event after the DELETE must not lose it."""
    document = _draft(db, booking)
    version = document.version

    delete_draft(db, document, actor="staff:test")

    event = db.scalars(
        select(BookingEvent).where(
            BookingEvent.booking_id == booking.id,
            BookingEvent.event_type == "document_deleted",
        )
    ).one()
    assert event.old_value == str(version)
    assert event.actor == "staff:test"


def test_abandoning_a_revise_still_restores_the_previous_version(db, booking):
    """The restore reads run AFTER the delete and are order-dependent on it
    -- get_current must come back None, which it only does once the row is
    gone. A fix that hoisted anything above the DELETE would silently
    disable this and leave the client's link dead."""
    from app.services.documents import revise

    original = _draft(db, booking)
    mark_sent(db, original, actor="staff:test")
    draft = revise(db, original, actor="staff:test")

    delete_draft(db, draft, actor="staff:test")

    current = get_current(db, booking.id, DocumentType.agreement)
    assert current is not None, "the client's link was left dead"
    assert current.id == original.id


# --- and the genuinely concurrent case ---------------------------------


@pytest.fixture()
def committed_draft():
    """Committed for real: two transactions racing is the whole point, so
    the savepoint-based `db` fixture cannot be used. Purged in a fixture
    rather than at the end of the test so it also runs on failure -- a test
    that leaks only when it fails arrives with a red suite and gets blamed
    on whatever is being built at the time."""
    setup = TestSessionLocal()
    slug = f"delete-race-{uuid.uuid4().hex[:8]}"
    venue = Venue(name="Delete Race Venue", slug=slug, reference_prefix=uuid.uuid4().hex[:5].upper())
    space = Space(
        venue=venue, name="Test Space", capacity=100, min_food_spend=0,
        standard_min_adults=0, wheelchair_accessible=False, has_per_head_shortfall_fee=True,
    )
    contact = Contact(name="Delete Race Contact", email=f"{slug}@example.com")
    setup.add_all([venue, space, contact])
    setup.commit()

    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 6, 12),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Delete Race",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    document = create_new_version(
        setup, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    mark_sent(setup, document, actor="test")
    setup.commit()
    ids = (booking.id, document.id, venue.id)
    setup.close()

    yield ids

    purge_venue(ids[2])


def test_a_signature_landing_mid_delete_wins(committed_draft):
    """Two real transactions, no shared session. The signer and the deleter
    both start while the document is sent; whatever the interleaving, the
    document must not end up deleted with a signature recorded nowhere.

    A locked re-read could not close this window without changing the
    module's lock order. The conditional DELETE does, because the check and
    the write are the same statement and the row lock serialises them.
    """
    booking_id, document_id, _venue_id = committed_draft
    start = threading.Barrier(2)
    outcomes = {}

    def signer():
        session = TestSessionLocal()
        try:
            doc = session.get(Document, document_id)
            start.wait(timeout=10)
            sign(session, doc, signer_name="A Client", signer_ip="1.2.3.4")
            session.commit()
            outcomes["sign"] = "ok"
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            outcomes["sign"] = repr(exc)
        finally:
            session.close()

    def deleter():
        session = TestSessionLocal()
        try:
            doc = session.get(Document, document_id)
            # The staleness the route has: read once, act later.
            doc.__dict__["status"] = DocumentStatus.draft
            start.wait(timeout=10)
            delete_draft(session, doc, actor="staff:racer")
            outcomes["delete"] = "ok"
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            outcomes["delete"] = repr(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=signer), threading.Thread(target=deleter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert set(outcomes) == {"sign", "delete"}, (
        f"a thread never finished, so nothing raced: {outcomes}"
    )

    check = TestSessionLocal()
    try:
        survivor = check.get(Document, document_id)
        if outcomes.get("sign") == "ok":
            assert survivor is not None, (
                f"the signed document was deleted anyway: {outcomes}"
            )
            assert survivor.signer_name == "A Client"
        else:
            # The signature lost the race; the delete is then legitimate.
            assert outcomes.get("delete") == "ok" or survivor is not None, outcomes
    finally:
        check.close()
