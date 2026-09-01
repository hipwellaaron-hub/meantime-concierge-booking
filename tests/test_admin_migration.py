"""The staff-only /admin/migration page: read-only report, then an import
bound to that exact file by hash, with strict header validation and a typed
confirmation. A production write path -- these guards are the safety."""

import csv
import datetime as dt
import io
import re

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Contact
from app.models.booking import BookingStatus
from app.services.booking import create_booking

HEADERS = [
    "booking_code", "event_date", "day", "event_name", "event_type", "space", "start_time", "end_time",
    "pax", "status", "contact_name", "contact_phone", "contact_email", "company", "opportunity_created",
    "pricing_locked_at", "pricing_basis", "lead_source", "food_total", "total_revenue", "total_paid",
    "total_outstanding", "deposit_paid", "beo_number", "coordinator", "layout", "comments",
    "missing_email", "needs_review",
]


def _row(**over):
    base = {h: "" for h in HEADERS}
    base.update(
        booking_code="AAA111", event_date="2026-11-14", event_name="Test Party", space="The Loft",
        start_time="18:00", end_time="23:30", pax="60", status="Confirmed", contact_name="Sam Jones",
        contact_email="sam@example.com", opportunity_created="2026-03-14", pricing_locked_at="2026-03-14",
        total_paid="500.00", deposit_paid="YES",
    )
    base.update(over)
    return base


def _csv_bytes(rows, headers=HEADERS) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8-sig")


@pytest.fixture()
def pub_client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def _csrf(client) -> str:
    page = client.get("/admin/migration")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_page_requires_staff(pub_client):
    resp = pub_client.get("/admin/migration")
    assert resp.status_code in (302, 303, 307)  # bounced to login, not 200


def test_report_is_read_only_and_shows_hash(admin_client, db, hamilton):
    before = db.query(Booking).count()
    resp = admin_client.post(
        "/admin/migration/report",
        data={"csrf_token": _csrf(admin_client)},
        files={"file": ("import.csv", _csv_bytes([_row()]), "text/csv")},
    )
    assert resp.status_code == 200
    assert "data rows" in resp.text and "would create" in resp.text
    db.expire_all()
    assert db.query(Booking).count() == before  # nothing written


def test_wrong_headers_are_refused(admin_client, db, hamilton):
    bad = b"name,date,amount\nfoo,2027-01-01,500\n"
    resp = admin_client.post(
        "/admin/migration/report",
        data={"csrf_token": _csrf(admin_client)},
        files={"file": ("wrong.csv", bad, "text/csv")},
    )
    assert resp.status_code == 422
    assert "missing expected columns" in resp.text


def test_import_needs_the_confirm_word(admin_client, db, hamilton):
    csrf = _csrf(admin_client)
    body = _csv_bytes([_row()])
    admin_client.post("/admin/migration/report", data={"csrf_token": csrf}, files={"file": ("i.csv", body, "text/csv")})
    resp = admin_client.post(
        "/admin/migration/import",
        data={"csrf_token": csrf, "confirm": ""},
        files={"file": ("i.csv", body, "text/csv")},
    )
    assert resp.status_code == 422
    assert db.query(Booking).filter_by(migration_external_ref="AAA111").count() == 0


def test_import_refuses_a_file_that_differs_from_the_report(admin_client, db, hamilton):
    csrf = _csrf(admin_client)
    reported = _csv_bytes([_row(booking_code="AAA111")])
    admin_client.post("/admin/migration/report", data={"csrf_token": csrf}, files={"file": ("a.csv", reported, "text/csv")})

    tampered = _csv_bytes([_row(booking_code="AAA111", pax="999")])  # same code, different bytes -> different hash
    resp = admin_client.post(
        "/admin/migration/import",
        data={"csrf_token": csrf, "confirm": "IMPORT"},
        files={"file": ("a.csv", tampered, "text/csv")},
    )
    assert resp.status_code == 422
    assert "hash" in resp.text.lower() or "match" in resp.text.lower()
    assert db.query(Booking).filter_by(migration_external_ref="AAA111").count() == 0


def test_import_happy_path_writes_the_reported_file(admin_client, db, hamilton):
    csrf = _csrf(admin_client)
    body = _csv_bytes([_row(booking_code="AAA111")])
    admin_client.post("/admin/migration/report", data={"csrf_token": csrf}, files={"file": ("a.csv", body, "text/csv")})
    resp = admin_client.post(
        "/admin/migration/import",
        data={"csrf_token": csrf, "confirm": "IMPORT"},
        files={"file": ("a.csv", body, "text/csv")},
    )
    assert resp.status_code == 200
    assert "Import complete" in resp.text
    db.expire_all()
    assert db.query(Booking).filter_by(migration_external_ref="AAA111").count() == 1


def _archived_booking(db, loft) -> Booking:
    c = Contact(name="Arch Client", email="arch@example.com", phone=None)
    db.add(c)
    db.flush()
    b = create_booking(
        db, space_id=loft.id, contact_id=c.id, event_date=dt.date(2026, 9, 12),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Archived Event",
        event_type="Birthday", adult_count=50, child_count=0, notes=None, actor="staff",
    )
    b.status = BookingStatus.archived
    db.commit()
    return b


def test_restore_archived_booking(admin_client, db, hamilton, loft):
    b = _archived_booking(db, loft)
    resp = admin_client.post(
        "/admin/migration/unarchive",
        data={"csrf_token": _csrf(admin_client), "references": b.reference_code, "confirm": "RESTORE"},
    )
    assert resp.status_code == 200
    assert "Restored" in resp.text
    db.expire_all()
    assert db.get(Booking, b.id).status == BookingStatus.confirmed


def test_restore_needs_confirm_word(admin_client, db, hamilton, loft):
    b = _archived_booking(db, loft)
    resp = admin_client.post(
        "/admin/migration/unarchive",
        data={"csrf_token": _csrf(admin_client), "references": b.reference_code, "confirm": ""},
    )
    assert resp.status_code == 422
    db.expire_all()
    assert db.get(Booking, b.id).status == BookingStatus.archived  # unchanged


def test_restore_reports_unknown_reference(admin_client, db, hamilton):
    resp = admin_client.post(
        "/admin/migration/unarchive",
        data={"csrf_token": _csrf(admin_client), "references": "HAM-NOPE-00000", "confirm": "RESTORE"},
    )
    assert resp.status_code == 200
    assert "Not restored" in resp.text
