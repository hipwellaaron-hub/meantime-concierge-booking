"""Proves the double-click-sign race is closed: two genuinely concurrent
POSTs to /d/{token}/sign for the same document can never both succeed.
Uses real threads and separate sessions/transactions (not the
savepoint-based `db` fixture) because the whole point is to exercise real
concurrent transactions -- same pattern as test_double_booking.py.
"""

import datetime as dt
import threading
import uuid

from app.models import Contact, Space, Venue
from app.models.document import DocumentStatus, DocumentType
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content
from app.services.documents import create_new_version, get_by_token, mark_sent, sign
import pytest

from tests.conftest import TestSessionLocal, purge_venue


@pytest.fixture()
def signable_document():
    """Committed for real -- concurrent transactions are the whole point, so
    the savepoint-based `db` fixture cannot be used here.

    Which means these rows SURVIVE the test, and until 2026-09-12 nothing
    removed them: this module alone had leaked 41 venues into the shared test
    database. A fixture rather than a cleanup block at the end of the test,
    so it also runs when an assertion fails -- a test that leaks only when it
    fails is the worst version, because the leak then arrives with a red
    suite and gets blamed on whatever is being built at the time.
    """
    setup = TestSessionLocal()
    venue = Venue(name="Sign Concurrency Test Venue", slug=f"sign-concurrency-test-{uuid.uuid4().hex[:8]}", reference_prefix=uuid.uuid4().hex[:5].upper())
    space = Space(
        venue=venue,
        name="Test Space",
        capacity=100,
        min_food_spend=0,
        standard_min_adults=0,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    )
    contact = Contact(name="Sign Concurrency Test Contact", email="sign-concurrency@example.com")
    setup.add_all([venue, space, contact])
    setup.commit()

    booking = create_booking(
        setup,
        space_id=space.id,
        contact_id=contact.id,
        event_date=dt.date(2027, 3, 6),
        start_time=dt.time(12, 0),
        end_time=dt.time(17, 0),
        event_name="Concurrency Sign Test",
        event_type="party",
        adult_count=10,
        child_count=0,
        notes=None,
        actor="test",
    )
    document = create_new_version(setup, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(setup, document, actor="test")
    token = document.access_token
    venue_id, contact_id = venue.id, contact.id
    setup.close()

    try:
        yield token
    finally:
        purge_venue(venue_id, contact_ids=[contact_id])


def test_concurrent_signs_cannot_both_succeed(signable_document):
    token = signable_document

    barrier = threading.Barrier(2)
    results = {}

    def attempt(key: str, signer_name: str):
        session = TestSessionLocal()
        try:
            doc = get_by_token(session, token)
            barrier.wait(timeout=5)
            sign(session, doc, signer_name=signer_name, signer_ip=f"10.0.0.{key}")
            results[key] = "success"
        except ValueError:
            results[key] = "failed"
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=("1", "Alice Signer")),
        threading.Thread(target=attempt, args=("2", "Bob Signer")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["failed", "success"], results

    verify = TestSessionLocal()
    final = get_by_token(verify, token)
    assert final.status == DocumentStatus.signed
    # exactly one of the two names won -- never a mix, never both applied
    assert final.signer_name in ("Alice Signer", "Bob Signer")
    verify.close()
