import datetime as dt
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking
from app.models.booking import BookingStatus
from app.services.booking import change_status, create_booking


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _booking(db, space, *, event_date, event_name):
    booking = create_booking(
        db, space_id=space.id, contact_id=None, event_date=event_date, event_name=event_name,
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    return booking


def test_returns_503_when_secret_not_configured(db):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", None):
            resp = client.post(
                "/internal/maintenance/archive-bookings-before", params={"cutoff_date": "2026-09-30"}
            )
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_rejects_missing_or_wrong_secret(db):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp_missing = client.post(
                "/internal/maintenance/archive-bookings-before", params={"cutoff_date": "2026-09-30"}
            )
            resp_wrong = client.post(
                "/internal/maintenance/archive-bookings-before",
                params={"cutoff_date": "2026-09-30"},
                headers={"x-maintenance-secret": "wrong"},
            )
        assert resp_missing.status_code == 403
        assert resp_wrong.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_archives_with_correct_secret(db, loft):
    from app.api import internal_maintenance

    booking = _booking(db, loft, event_date=dt.date(2026, 8, 1), event_name="Pre-Launch Test")

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp = client.post(
                "/internal/maintenance/archive-bookings-before",
                params={"cutoff_date": "2026-09-30"},
                headers={"x-maintenance-secret": "real-secret"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["archived_count"] == 1
        assert body["bookings"][0]["reference_code"] == booking.reference_code
    finally:
        app.dependency_overrides.clear()

    db.refresh(booking)
    assert booking.status == BookingStatus.archived


def test_rejects_malformed_cutoff_date(db):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp = client.post(
                "/internal/maintenance/archive-bookings-before",
                params={"cutoff_date": "not-a-date"},
                headers={"x-maintenance-secret": "real-secret"},
            )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()
