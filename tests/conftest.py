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
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


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
    from app.api.wizard import _wizard_step_rate_limiter

    _enquiry_rate_limiter._hits.clear()
    _sign_rate_limiter._hits.clear()
    _wizard_step_rate_limiter._hits.clear()
    login_rate_limiter._hits.clear()
    yield
    _enquiry_rate_limiter._hits.clear()
    _sign_rate_limiter._hits.clear()
    _wizard_step_rate_limiter._hits.clear()
    login_rate_limiter._hits.clear()
