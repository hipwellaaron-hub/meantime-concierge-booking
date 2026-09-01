"""Legacy document/invoice uploads (app.services.legacy_documents + routes).

Covers the three things that matter most: a legacy placeholder must NEVER
render to a client (both the HTML and PDF public routes), a legacy deposit
must not double-count against a final invoice generated later, and legacy
rows are inert (no regenerate / revise / payment). Plus upload validation,
the booking-change mismatch warning, and the admin upload/serve round-trip.
"""

import datetime as dt
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Contact
from app.models.invoice import InvoiceStatus, InvoiceType
from app.services import invoicing, legacy_documents
from app.services.booking import create_booking, has_paid_deposit, has_signed_agreement
from app.services.documents import create_new_version
from app.models.document import DocumentType

PDF = b"%PDF-1.4\n%fake but valid header\n"


@pytest.fixture()
def pub_client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def _booking(db, loft, *, when=dt.date(2026, 11, 14)) -> Booking:
    c = Contact(name="Legacy Client", email="legacy@example.com", phone=None)
    db.add(c)
    db.flush()
    return create_booking(
        db, space_id=loft.id, contact_id=c.id, event_date=when,
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name="Legacy Event",
        event_type="Birthday", adult_count=60, child_count=0, notes=None, actor="staff",
    )


# --- view guard: a legacy placeholder must never reach a client ---------------


def test_client_html_route_404s_a_legacy_document(pub_client, db, loft):
    b = _booking(db, loft)
    doc = legacy_documents.attach_agreement_pdf(db, b, pdf=PDF, filename="a.pdf", source_ref="IVVY1", actor="staff")
    resp = pub_client.get(f"/d/{doc.access_token}")
    assert resp.status_code == 404


def test_client_pdf_route_404s_a_legacy_document(pub_client, db, loft):
    b = _booking(db, loft)
    doc = legacy_documents.attach_agreement_pdf(db, b, pdf=PDF, filename="a.pdf", source_ref="IVVY1", actor="staff")
    resp = pub_client.get(f"/d/{doc.access_token}/pdf")
    assert resp.status_code == 404


def test_client_invoice_routes_404_a_legacy_deposit(pub_client, db, loft):
    b = _booking(db, loft)
    inv = legacy_documents.attach_deposit_pdf(db, b, pdf=PDF, filename="d.pdf", amount=Decimal("500.00"), source_ref="IVVY1", actor="staff")
    assert pub_client.get(f"/i/{inv.access_token}").status_code == 404
    assert pub_client.get(f"/i/{inv.access_token}/pdf").status_code == 404


# --- double count: legacy deposit first, final invoice months later -----------


def test_legacy_deposit_not_double_counted_by_a_later_final_invoice(db, loft):
    b = _booking(db, loft)
    # 1) legacy deposit exists first (as it will for the 43 migrated bookings)
    legacy_documents.attach_deposit_pdf(
        db, b, pdf=PDF, filename="dep.pdf", amount=Decimal("500.00"), source_ref="IVVY1", actor="staff"
    )
    assert invoicing.get_deposit_paid(db, b) == Decimal("500.00")

    # 2) months later, the final invoice is generated for the food balance
    final = invoicing.create_final_invoice(
        db, b, line_items=[{"description": "Food & beverage", "quantity": 1, "unit_price": "1500.00"}],
        due_date=b.event_date, actor="staff",
    )
    credit_lines = [li for li in final.line_items if Decimal(str(li["unit_price"])) < 0]
    assert len(credit_lines) == 1
    assert Decimal(str(credit_lines[0]["unit_price"])) == Decimal("-500.00")
    assert final.total == Decimal("1000.00")  # 1500 food - 500 deposit, credited exactly once
    assert invoicing.get_deposit_paid(db, b) == Decimal("500.00")  # not $1000


# --- gates + inertness --------------------------------------------------------


def test_legacy_agreement_and_deposit_satisfy_gates(db, loft):
    b = _booking(db, loft)
    legacy_documents.attach_agreement_pdf(db, b, pdf=PDF, filename="a.pdf", source_ref="IVVY1", actor="staff")
    legacy_documents.attach_deposit_pdf(db, b, pdf=PDF, filename="d.pdf", amount=Decimal("500.00"), source_ref="IVVY1", actor="staff")
    assert has_signed_agreement(db, b) is True
    assert has_paid_deposit(db, b) is True


def test_legacy_agreement_cannot_be_regenerated_over(db, loft):
    b = _booking(db, loft)
    legacy_documents.attach_agreement_pdf(db, b, pdf=PDF, filename="a.pdf", source_ref="IVVY1", actor="staff")
    with pytest.raises(ValueError, match="legacy"):
        create_new_version(db, b, DocumentType.agreement, {"x": 1}, actor="staff")


def test_legacy_deposit_cannot_take_a_payment(db, loft):
    b = _booking(db, loft)
    inv = legacy_documents.attach_deposit_pdf(db, b, pdf=PDF, filename="d.pdf", amount=Decimal("500.00"), source_ref="IVVY1", actor="staff")
    from app.models.payment import PaymentMethod
    with pytest.raises(ValueError, match="legacy"):
        invoicing.record_payment(db, inv, amount=Decimal("100.00"), method=PaymentMethod.cash, actor="staff")


def test_legacy_deposit_invoice_cannot_be_revised(db, loft):
    b = _booking(db, loft)
    # an outstanding (sent) legacy deposit
    inv = legacy_documents.attach_deposit_pdf(db, b, pdf=PDF, filename="d.pdf", amount=Decimal("500.00"), source_ref="IVVY1", actor="staff")
    inv.status = InvoiceStatus.sent
    db.flush()
    with pytest.raises(ValueError, match="legacy"):
        invoicing.revise_sent_invoice(db, inv, actor="staff")


# --- upload validation --------------------------------------------------------


def test_upload_rejects_non_pdf(db, loft):
    b = _booking(db, loft)
    with pytest.raises(legacy_documents.LegacyUploadError, match="PDF"):
        legacy_documents.attach_agreement_pdf(db, b, pdf=b"not a pdf at all", filename="x.pdf", actor="staff")


def test_upload_rejects_oversize(db, loft):
    b = _booking(db, loft)
    too_big = b"%PDF-" + b"0" * (legacy_documents.MAX_PDF_BYTES + 1)
    with pytest.raises(legacy_documents.LegacyUploadError, match="too large"):
        legacy_documents.attach_agreement_pdf(db, b, pdf=too_big, filename="x.pdf", actor="staff")


# --- mismatch warning when the booking changes --------------------------------


def test_snapshot_mismatch_flags_a_changed_booking(db, loft, mezzanine):
    b = _booking(db, loft, when=dt.date(2026, 11, 14))
    doc = legacy_documents.attach_agreement_pdf(db, b, pdf=PDF, filename="a.pdf", source_ref="IVVY1", actor="staff")
    assert legacy_documents.snapshot_mismatch(doc, b) is None  # matches at upload

    b.event_date = dt.date(2026, 11, 21)  # date moved after the PDF was signed
    b.space_id = mezzanine.id
    db.flush()
    db.refresh(b)
    msg = legacy_documents.snapshot_mismatch(doc, b)
    assert msg and "date was 2026-11-14" in msg and "space was The Loft" in msg
    assert legacy_documents.legacy_mismatches(b)  # surfaced for the admin


# --- admin upload + serve round-trip -----------------------------------------


def test_admin_upload_and_serve_legacy_agreement(admin_client, db, loft):
    b = _booking(db, loft)
    detail = admin_client.get(f"/admin/bookings/{b.id}")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)

    up = admin_client.post(
        f"/admin/bookings/{b.id}/legacy-agreement",
        data={"csrf_token": csrf, "source_ref": "IVVY9", "signed_date": "2026-08-31"},
        files={"file": ("signed.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert up.status_code == 303

    db.expire_all()
    doc = next(d for d in db.get(Booking, b.id).documents if d.is_legacy)
    served = admin_client.get(f"/admin/bookings/{b.id}/documents/{doc.id}/legacy-file")
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/pdf"
    assert "attachment" in served.headers["content-disposition"]
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.content == PDF

    # the detail page renders the legacy agreement (badge + PDF download),
    # not the live view/regenerate controls
    detail2 = admin_client.get(f"/admin/bookings/{b.id}")
    assert detail2.status_code == 200
    assert "LEGACY" in detail2.text
    assert f"/admin/bookings/{b.id}/documents/{doc.id}/legacy-file" in detail2.text
