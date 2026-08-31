"""Proves the double-booking guarantee is structural, not just application
logic: two genuinely concurrent transactions racing to insert overlapping
bookings for the same space can never both succeed. This test does its own
setup/teardown with real commits (not the rollback-based `db` fixture)
because the whole point is to exercise real concurrent transactions.
"""

import datetime as dt
import threading
import uuid

from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import Space, Venue
from app.models.booking import Booking, BookingStatus
from tests.conftest import TestSessionLocal


def test_concurrent_overlapping_inserts_cannot_both_succeed():
    setup_session = TestSessionLocal()
    venue = Venue(name="Concurrency Test Venue", slug=f"concurrency-test-{uuid.uuid4().hex[:8]}")
    space = Space(
        venue=venue,
        name="Test Space",
        capacity=100,
        min_food_spend=0,
        standard_min_adults=0,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    )
    setup_session.add_all([venue, space])
    setup_session.commit()
    space_id, venue_id = space.id, venue.id
    setup_session.close()

    barrier = threading.Barrier(2)
    results = {}

    def attempt(key: str):
        session = TestSessionLocal()
        try:
            booking = Booking(
                space_id=space_id,
                event_date=dt.date(2027, 1, 1),
                start_time=dt.time(12, 0),
                end_time=dt.time(16, 0),
                status=BookingStatus.tentative,  # enquiry/offered are deliberately non-blocking
                event_name=f"Concurrent {key}",
                adult_count=10,
                child_count=0,
                agreed_min_adults=0,
                pricing_locked_at=dt.date.today(),
                reference_code=f"CONC-{key}-{uuid.uuid4().hex[:8].upper()}",
            )
            session.add(booking)
            barrier.wait(timeout=5)  # maximize actual overlap between the two transactions
            session.commit()
            results[key] = "success"
        except (IntegrityError, OperationalError):
            # Two mechanisms can reject the loser, and BOTH must count as a
            # failed attempt for the guarantee to hold:
            #  - IntegrityError (exclusion_violation): the loser saw the
            #    winner's already-committed overlapping row.
            #  - OperationalError (deadlock_detected, SQLSTATE 40P01): when
            #    both inserts reach the GiST exclusion check at the same
            #    instant, each waits on the other's transaction; Postgres
            #    breaks the symmetric wait by aborting one. The aborted txn
            #    inserted nothing.
            # Either way exactly one transaction commits, so the invariant
            # "two overlapping bookings can never BOTH succeed" is proven
            # regardless of which mechanism Postgres uses to settle the race.
            session.rollback()
            results[key] = "failed"
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(key,)) for key in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["failed", "success"], results

    cleanup = TestSessionLocal()
    cleanup.query(Booking).filter(Booking.space_id == space_id).delete()
    cleanup.query(Space).filter(Space.id == space_id).delete()
    cleanup.query(Venue).filter(Venue.id == venue_id).delete()
    cleanup.commit()
    cleanup.close()
