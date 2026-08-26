from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _row(**overrides):
    row = {
        "Date": "02/10/2026",
        "Booking Code": "ABC123",
        "Booking Name": "Reuben's 18th",
        "Start Time": "6:00 pm",
        "End Time": "11:30 pm",
        "Space": "The Loft",
        "Status": "Confirmed",
        "Pax": "130",
        "Contact Name": "Reuben Pedonese",
        "Contact Phone": "0491710339",
    }
    row.update(overrides)
    return row


def test_rejects_missing_or_wrong_secret(db):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp = client.post("/internal/maintenance/import-ivvy-calendar-rows", json={"rows": []})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_imports_rows_with_correct_secret(db, hamilton, loft):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp = client.post(
                "/internal/maintenance/import-ivvy-calendar-rows",
                json={"rows": [_row()]},
                headers={"x-maintenance-secret": "real-secret"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 1
        assert body["errors"] == []
    finally:
        app.dependency_overrides.clear()

    assert db.query(Booking).filter_by(migration_external_ref="ABC123").count() == 1


def test_rejects_malformed_body(db):
    from app.api import internal_maintenance

    client = _client(db)
    try:
        with patch.object(internal_maintenance, "MAINTENANCE_SECRET", "real-secret"):
            resp = client.post(
                "/internal/maintenance/import-ivvy-calendar-rows",
                json={"not_rows": []},
                headers={"x-maintenance-secret": "real-secret"},
            )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()
