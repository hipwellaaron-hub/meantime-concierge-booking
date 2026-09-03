"""Reconciliation findings on the Triage page, and the run-now button.

The findings table existed before this, but nothing rendered it. A finding
nobody can see is not a finding, and the NOTES_BEFORE_BEO check in
particular only earns its keep if the text it found is readable on a
staff-only page rather than one booking at a time.
"""

import datetime as dt
import re

from app.models import Contact, ReconciliationFinding
from app.services import reconciliation
from app.services.booking import create_booking

PRIVATE = "Client mentioned they are going through a divorce, keep the seating simple."
CLIENT = "Hi, we would love the Loft on the 14th if it is free, about 80 of us."


def _booking(db, loft, *, notes=None, enquiry_text=None, reference=None):
    contact = Contact(name="Triage Client", email="triage.client@example.com")
    db.add(contact)
    db.flush()
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=50),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_name="Triage Test", event_type="corporate",
        adult_count=80, child_count=0, notes=notes, actor="staff:test",
        enquiry_text=enquiry_text,
    )
    if reference:
        b.reference_code = reference
        db.commit()
    return b


def _csrf(client) -> str:
    page = client.get("/admin/triage")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_the_page_renders_with_no_findings(admin_client, db, hamilton):
    resp = admin_client.get("/admin/triage")
    assert resp.status_code == 200
    assert "Reconciliation findings" in resp.text
    assert "Run reconciliation now" in resp.text


def test_run_now_opens_findings_and_they_appear_on_the_page(admin_client, db, hamilton, loft):
    _booking(db, loft, reference="HAM-TBD-TRIAG")  # a TBD reference: a real finding
    assert db.query(ReconciliationFinding).count() == 0

    resp = admin_client.post(
        "/admin/triage/reconcile", data={"csrf_token": _csrf(admin_client)}, follow_redirects=False
    )
    assert resp.status_code == 303

    db.expire_all()
    assert db.query(ReconciliationFinding).filter_by(check_code="REFERENCE_TBD").count() == 1

    page = admin_client.get("/admin/triage").text
    assert "reference tbd" in page.lower()
    assert "HAM-TBD-TRIAG" in page


def test_notes_are_readable_on_the_page_not_just_counted(admin_client, db, hamilton, loft):
    """The point of NOTES_BEFORE_BEO: the text itself, on a staff-only page."""
    _booking(db, loft, notes=PRIVATE, enquiry_text=CLIENT)
    reconciliation.run(db, hamilton)

    page = admin_client.get("/admin/triage").text
    assert "Notes to read before an Event Order" in page
    assert PRIVATE in page
    assert CLIENT in page
    # And the two kinds of text are labelled as what they are.
    assert "client wrote" in page
    assert "internal" in page


def test_run_now_requires_csrf(admin_client, db, hamilton):
    resp = admin_client.post("/admin/triage/reconcile", data={}, follow_redirects=False)
    assert resp.status_code in (400, 403, 422)


def test_run_now_is_staff_only(db, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app, follow_redirects=False).post("/admin/triage/reconcile", data={})
        assert resp.status_code in (302, 303, 307, 401, 403)
        assert db.query(ReconciliationFinding).count() == 0
    finally:
        app.dependency_overrides.clear()


def test_the_dry_run_prints_the_notes(db, hamilton, loft, monkeypatch, capsys):
    """The CLI equivalent, for railway run: references and text, not counts."""
    from app import run_reconciliation as cli

    _booking(db, loft, notes=PRIVATE, enquiry_text=CLIENT, reference="HAM-20270101-CLI01")

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli, "SessionLocal", lambda: _Ctx())
    monkeypatch.setattr("sys.argv", ["run_reconciliation", "--dry-run"])
    assert cli.main() == 0

    out = capsys.readouterr().out
    assert "HAM-20270101-CLI01" in out
    assert "client wrote:" in out and CLIENT in out
    assert "internal:" in out and PRIVATE in out
    assert "nothing written" in out
