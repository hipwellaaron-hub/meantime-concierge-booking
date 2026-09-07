"""A lock that waits its turn can be handed nothing at all.

lock_current_for_update takes the row lock so a caller can read, decide,
and write without an approval landing in between. But when the transaction
it waits on was ITSELF a regenerate, the row it was blocked on is no longer
current by the time the lock is granted -- Postgres re-evaluates it under
EvalPlanQual, sees is_current has gone false, skips it, and the replacement
row is not in this statement's snapshot. The query comes back empty while a
current document plainly exists.

Empty is the dangerous answer, because every caller reads it as "this
booking has no document yet". losses() returns [] for a None document, so
no confirmation screen is shown, the new version replaces a declared
allergy with the generator's placeholder, and the audit trail records a
plain document_created with no regenerate note. Proved through the real
route with two threads: 303 where the same race with the retry in place
gives 409 and keeps the allergy.

Real sessions and real commits, not the savepoint `db` fixture: the whole
point is two transactions contending for one row, which a single
transaction's savepoints cannot show.

This repair existed on 2bbd23e and went away with the revert of that
commit. It is being re-done deliberately rather than rediscovered.
"""

import datetime as dt
import threading
import time
import uuid

import pytest

from app.models.document import DocumentType
from app.services import document_regeneration, documents as documents_service
from app.services.document_generation import generate_beo_content

ALLERGY = "1x severe nut allergy (table 4)."


def _setup(name):
    """A booking whose current BEO carries a declared allergy."""
    from app.models import Contact
    from app.seed import seed as seed_hamilton
    from app.services.booking import create_booking
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    space = next(sp for sp in venue.spaces if sp.is_bookable)
    contact = Contact(name=name, email=f"lockrace.{uuid.uuid4().hex[:8]}@example.com")
    setup.add(contact)
    setup.flush()
    booking = create_booking(
        setup, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 6, 11),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=f"{name} {uuid.uuid4().hex[:6]}",
        event_type="corporate", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking)
    content["dietaries"] = ALLERGY
    documents_service.create_new_version(setup, booking, DocumentType.beo, content, actor="staff:test")
    ids = (booking.id, type(booking))
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


def _run_the_race(booking_id, booking_type):
    """A regenerates and commits a new version while B is blocked on the
    old one's lock. Returns what B was handed, or what it raised."""
    from tests.conftest import TestSessionLocal

    barrier = threading.Barrier(2)
    out = {}

    def writer():
        session = TestSessionLocal()
        try:
            bk = session.get(booking_type, booking_id)
            locked = documents_service.lock_current_for_update(session, booking_id, DocumentType.beo)
            barrier.wait(timeout=10)
            # Held long enough for B's own locked read to start and block.
            time.sleep(0.4)
            documents_service.create_new_version(
                session, bk, DocumentType.beo, dict(locked.content), actor="staff:aaron"
            )
            out["A_wrote"] = True
        except Exception as exc:  # noqa: BLE001
            out["A_error"] = exc
        finally:
            session.close()

    def reader():
        session = TestSessionLocal()
        try:
            barrier.wait(timeout=10)
            # After A has the lock and before A commits, so this blocks.
            time.sleep(0.05)
            started = time.monotonic()
            current = documents_service.lock_current_for_update(session, booking_id, DocumentType.beo)
            out["B_waited"] = time.monotonic() - started
            out["B_received"] = current
            out["B_version"] = None if current is None else current.version
            out["B_dietaries"] = None if current is None else current.content.get("dietaries")
        except Exception as exc:  # noqa: BLE001
            out["B_error"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


@pytest.mark.usefixtures("hamilton")
def test_a_locked_read_blocked_by_a_regenerate_is_handed_the_new_version():
    """The defect, stated as the thing that must be true: B waited on A's
    lock, so B must be given what A wrote -- with the allergy still on it.

    Never "no exception was raised": the defect IS silent success. B was
    handed None, which reads as "no document", and the regenerate that
    asked for the lock went on to overwrite the allergy without asking.
    """
    booking_id, booking_type = _setup("Lock Race")
    try:
        out = _run_the_race(booking_id, booking_type)

        assert "A_error" not in out, out
        assert "B_error" not in out, out
        assert out["B_waited"] > 0.2, f"B never actually blocked on the lock: {out['B_waited']}s"
        assert out["B_received"] is not None, "handed None while a current document exists"
        assert out["B_version"] == 2, out
        assert out["B_dietaries"] == ALLERGY, out
    finally:
        _purge(booking_type, booking_id)


@pytest.mark.usefixtures("hamilton")
def test_running_out_of_attempts_says_so_instead_of_answering_none(monkeypatch):
    """The bound has to fail loudly. With one attempt the same race cannot
    be recovered -- and the answer must be an error a staff member sees,
    not the None that reads as "no document exists" and overwrites it.

    One attempt is exactly the reverted commit's `for _ in range(2)` minus
    its single retry, which is why the loop is bounded at more than that:
    the window narrows with each attempt but only the honest failure
    closes it.
    """
    monkeypatch.setattr(documents_service, "_LOCK_ATTEMPTS", 1)
    booking_id, booking_type = _setup("Lock Race Exhausted")
    try:
        out = _run_the_race(booking_id, booking_type)

        assert "A_error" not in out, out
        assert out.get("B_received", "not set") == "not set", f"B answered {out.get('B_received')!r}"
        error = out.get("B_error")
        assert isinstance(error, RuntimeError), out
        assert str(booking_id) in str(error) and "beo" in str(error), str(error)
    finally:
        _purge(booking_type, booking_id)


def test_no_document_at_all_is_still_a_plain_none(db, booking):
    """The retry must not turn a first generate into an error. A booking
    with no BEO yet has genuinely nothing to lock, and None is the right
    answer -- confirmed by an unlocked read before it is given."""
    assert documents_service.lock_current_for_update(db, booking.id, DocumentType.beo) is None


def test_why_none_is_the_dangerous_answer(db, booking):
    """What the caller does with it, pinned here so the stakes cannot be
    edited out of the helper's docstring alone: with no current document
    there is nothing to compare against, so nothing is ever at risk and
    the regenerate writes straight through."""
    assert document_regeneration.losses(db, None, {"dietaries": ALLERGY}) == []
