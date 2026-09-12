import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Contact
from app.seed import seed as seed_hamilton
from app.seed_catalogue import seed as seed_catalogue
from app.seed_public_holidays import seed as seed_public_holidays
from app.services.booking import create_booking

test_engine = create_engine(settings.test_database_url)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db():
    """Each test runs inside an outer transaction that is always rolled
    back, no matter how many times the code under test calls commit() —
    those commits just release/recreate a SAVEPOINT instead of touching
    the real transaction."""
    connection = test_engine.connect()
    outer_transaction = connection.begin()
    session = TestSessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture()
def hamilton(db):
    return seed_hamilton(db)


@pytest.fixture()
def loft(hamilton, db):
    return next(s for s in hamilton.spaces if s.name == "The Loft")


@pytest.fixture()
def lounge(hamilton, db):
    return next(s for s in hamilton.spaces if s.name == "The Lounge")


@pytest.fixture()
def mezzanine(hamilton, db):
    return next(s for s in hamilton.spaces if s.name == "The Mezzanine")


@pytest.fixture()
def menu_items(db):
    seed_catalogue(db)
    from app.models import MenuItem

    return {item.name: item for item in db.query(MenuItem)}


@pytest.fixture()
def unassigned_space(hamilton, db):
    from app.seed import UNASSIGNED_SPACE_NAME

    return next(s for s in hamilton.spaces if s.name == UNASSIGNED_SPACE_NAME)


@pytest.fixture()
def contact(db):
    c = Contact(name="Pat Wilson", email="pat.wilson@example.com", phone="0400000000")
    db.add(c)
    db.flush()
    return c


@pytest.fixture()
def booking(db, loft, contact):
    return create_booking(
        db,
        space_id=loft.id,
        contact_id=contact.id,
        event_date=dt.date(2026, 10, 3),
        start_time=dt.time(12, 0),
        end_time=dt.time(17, 0),
        event_name="Wilson Wedding",
        event_type="wedding",
        adult_count=80,
        child_count=5,
        notes="Bride requests no seafood.",
        actor="test",
    )


@pytest.fixture()
def public_holidays(db):
    seed_public_holidays(db)


STAFF_TEST_PASSWORD = "testpassword123"


@pytest.fixture()
def staff_user(db):
    from app.services.staff_auth import create_or_update_staff_user

    return create_or_update_staff_user(db, email="staff@meantime.com.au", name="Test Staff", password=STAFF_TEST_PASSWORD)


@pytest.fixture()
def admin_client(db, staff_user, hamilton):
    """A TestClient already logged in as staff_user, with get_db overridden
    to this test's transactional session. Every admin route needs the same
    login dance (GET the form to obtain a session-bound csrf_token, then
    POST it back) -- centralized here rather than repeated per test."""
    import re

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    login_page = client.get("/admin/login")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    resp = client.post(
        "/admin/login",
        data={"csrf_token": csrf_token, "email": staff_user.email, "password": STAFF_TEST_PASSWORD, "next": "/admin/"},
    )
    assert resp.status_code in (200, 303)

    _scope_admin_urls(client, hamilton.slug)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _scope_admin_urls(client, venue_slug: str):
    """Rewrite /admin/<section> to /admin/<venue>/<section> for the sections
    that have MOVED onto the venue segment.

    ONE place instead of editing every admin test, which is the trade the
    venue-switch design chose deliberately -- and the cost is named rather
    than hidden: these tests stop asserting WHICH URL the app serves. Three
    things assert that instead, and they are the reason this is safe:

      * tests/route_inventory.txt -- a checked-in list of all 119 routes that
        fails on any change
      * test_the_nav_never_points_at_a_section_that_has_not_moved
      * test_every_nav_link_actually_resolves, which follows every nav link

    Driven by the app's own MOVED_SECTIONS, so a section the app has not
    moved is NOT rewritten here either -- otherwise this fixture would send
    every bookings test to a URL that does not exist yet, and the failure
    would look like a broken route rather than a half-finished rollout.
    """
    from app.venue_scope import MOVED_SECTIONS

    prefixes = sorted((s for s in MOVED_SECTIONS if s), key=len, reverse=True)
    original = client.request

    def request(method, url, *args, **kwargs):
        if isinstance(url, str) and url.startswith("/admin/"):
            rest = url[len("/admin"):]
            for section in prefixes:
                if rest == section or rest.startswith(section + "/") or rest.startswith(section + "?"):
                    url = f"/admin/{venue_slug}{rest}"
                    break
        return original(method, url, *args, **kwargs)

    client.request = request


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """The in-memory rate limiters (app.rate_limit) are module-level
    singletons so they work correctly across requests in the real running
    process -- but that means their state would otherwise leak between
    tests in this same pytest process, causing unrelated tests to trip a
    429 that has nothing to do with what they're testing."""
    from app.api.admin_auth import login_rate_limiter
    from app.api.documents import _sign_rate_limiter
    from app.api.enquiries import _enquiry_rate_limiter
    from app.api.staff_app import _app_login_rate_limiter
    from app.api.wizard import _wizard_step_rate_limiter

    _app_login_rate_limiter._hits.clear()
    _enquiry_rate_limiter._hits.clear()
    _sign_rate_limiter._hits.clear()
    _wizard_step_rate_limiter._hits.clear()
    login_rate_limiter._hits.clear()
    yield
    _app_login_rate_limiter._hits.clear()
    _enquiry_rate_limiter._hits.clear()
    _sign_rate_limiter._hits.clear()
    _wizard_step_rate_limiter._hits.clear()
    login_rate_limiter._hits.clear()


@pytest.fixture(autouse=True)
def _background_work_uses_the_test_session(db, monkeypatch):
    """documents.deliver_beo_approval_emails runs after the response with a
    session of its own (documents.background_db). Under test it gets the
    fixture session, so its writes land in the transaction the test can
    see -- and so no test can reach the real database through a background
    task by approving an Event Order through the route."""
    from contextlib import nullcontext

    from app.services import documents as documents_service

    monkeypatch.setattr(documents_service, "background_db", lambda: nullcontext(db))


# --- cleanup for the tests that deliberately COMMIT -------------------------
#
# Four test modules exercise real concurrent transactions, so they cannot use
# the `db` fixture (which rolls everything back inside one savepoint) and have
# to commit against their own sessions. Until 2026-09-12 none of them cleaned
# up, and the shared test database had accumulated 99 leaked venues, 351
# contacts, 81 bookings, 34 documents and 33 invoices.
#
# That is not tidiness. The leaked rows made `Venue`/`Contact` lookups that
# expect one row raise MultipleResultsFound in UNRELATED tests, and any query
# that counts or lists across the table reads as a bug in whatever is being
# built at the time -- which is exactly the shape of question the venue switch
# asks ("does this list contain another venue's rows?").

def purge_venue(venue_id, *, contact_ids=()):
    """Delete a committed test venue and everything hanging off it.

    Call from a fixture's `finally` so a failing assertion still cleans up --
    a test that leaks only when it fails is the worst version, because the
    leak then arrives with a red suite and gets blamed on the change under
    test.

    Uses `delete_booking_and_dependents`, which is the codebase's ONE
    sanctioned hard delete, rather than a second delete graph that would
    drift from it. It already knows the FK-safe order, and it already sets
    `app.allow_booking_purge` transaction-locally -- `booking_events` is
    append-only at the database level (a trigger from the phase-1 schema,
    given this deliberate escape hatch by c4f1a9d2e6b8), so nothing else can
    remove a booking at all.

    Contacts are passed in explicitly: `contacts` has no venue_id -- a person
    is not owned by a venue -- so nothing in the graph can reach them.
    """
    from sqlalchemy import select, text

    from app.models import Booking, Space, Venue
    from app.services.booking import delete_booking_and_dependents

    session = TestSessionLocal()
    try:
        bookings = session.scalars(
            select(Booking).join(Space, Space.id == Booking.space_id)
            .where(Space.venue_id == venue_id)
            # Parents first: the helper refuses a linked child directly and
            # removes it via its parent, so a child reached first would raise.
            .where(Booking.parent_booking_id.is_(None))
        ).all()
        for booking in bookings:
            delete_booking_and_dependents(session, booking, actor="test-cleanup")

        # Anything left is a linked child whose parent was already removed,
        # or a booking created by a direct INSERT with no parent link.
        for booking in session.scalars(
            select(Booking).join(Space, Space.id == Booking.space_id).where(Space.venue_id == venue_id)
        ).all():
            session.execute(text("SET LOCAL app.allow_booking_purge = 'on'"))
            session.delete(booking)
        session.commit()

        session.execute(text("DELETE FROM spaces WHERE venue_id = :v"), {"v": venue_id})
        session.execute(text("DELETE FROM venues WHERE id = :v"), {"v": venue_id})
        if contact_ids:
            session.execute(
                text("DELETE FROM contacts WHERE id = ANY(:ids)"), {"ids": list(contact_ids)}
            )
        session.commit()
    finally:
        session.close()


@pytest.fixture(scope="session", autouse=True)
def _sweep_committed_leftovers():
    """Last line of defence for the tests that COMMIT.

    Contacts get swept rather than owned: `contacts` has no venue_id and no
    booking ownership -- a person is not owned by a booking, so
    delete_booking_and_dependents correctly leaves them behind, and there is
    no single place a per-test fixture could hang. Only ORPHANS go: a contact
    with no bookings is referenced by nothing, by definition.

    Leaked VENUES are only REPORTED, never swept. A venue has an owner -- the
    fixture that created it -- so quietly deleting one would hide a test that
    forgot to clean up, and the next person would rediscover the same 99-row
    pile-up. Loud and left alone beats tidy and invisible.
    """
    yield

    from sqlalchemy import text

    with test_engine.begin() as conn:
        orphans = conn.execute(
            text("DELETE FROM contacts WHERE id NOT IN "
                 "(SELECT contact_id FROM bookings WHERE contact_id IS NOT NULL)")
        ).rowcount
        leaked = [
            r[0] for r in conn.execute(text("SELECT slug FROM venues WHERE slug <> 'hamilton'"))
        ]
    if orphans:
        print(f"\n[cleanup] swept {orphans} orphaned contact(s) left by committing tests")
    if leaked:
        print(
            f"\n[cleanup] WARNING: {len(leaked)} venue(s) leaked into the test database and were "
            f"NOT swept: {', '.join(leaked[:10])}"
            "\n           A committing test is missing its purge_venue() cleanup. Left in place "
            "deliberately so it stays visible."
        )
