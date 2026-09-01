"""One-time staff-only page to run the iVvy -> Concierge migration against
the live database by uploading the export CSV.

Deliberately kept OFF the source tree's normal flow and meant to be REMOVED
once the changeover is done -- a production write path that takes an
uploaded file and creates bookings, invoices and payments must not sit
around dormant. See app/main.py where it is registered.

Safety rails, all four requested:
- The import is bound to the report by SHA-256: you can only import the
  exact file you just ran the read-only report on (hash stored in the
  session by /report, re-checked by /import).
- The header set is validated strictly (app.services.concierge_migration.
  validate_headers) -- a wrong file is refused whole, never half-imported.
- Staff-only + CSRF, and the import needs the word IMPORT typed to confirm.
- The uploaded bytes are held in memory / a temp file only, never committed
  anywhere -- client PII never reaches git.
"""

import csv
import hashlib
import io
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.admin_auth import admin_ctx, require_csrf, require_staff
from app.database import get_db
from app.models import Venue
from app.models.staff_user import StaffUser
from app.services import concierge_migration
from app.templating import templates

router = APIRouter(prefix="/admin/migration", tags=["admin-migration"], dependencies=[Depends(require_staff)])

_MAX_BYTES = 5 * 1024 * 1024  # the export is a few KB; cap defensively
_SESSION_HASH_KEY = "migration_report_hash"


def _venue(db: Session) -> Venue:
    return db.query(Venue).filter_by(slug="hamilton").one()


def _read_hash_rows(file: UploadFile) -> tuple[bytes, str, int]:
    data = file.file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=422, detail="File too large")
    digest = hashlib.sha256(data).hexdigest()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    rows = sum(1 for r in reader if (r.get("booking_code") or "").strip())
    return data, digest, rows


def _to_tempfile(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


@router.get("", response_class=HTMLResponse)
def migration_page(request: Request, staff: StaffUser = Depends(require_staff)):
    return templates.TemplateResponse(request, "admin/migration.html", admin_ctx(request, staff))


@router.post("/report", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
def run_report(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    data, digest, rows = _read_hash_rows(file)
    path = _to_tempfile(data)
    try:
        report = concierge_migration.report_migration_csv(db, path, venue=_venue(db))
    except concierge_migration.MigrationInputError as exc:
        request.session.pop(_SESSION_HASH_KEY, None)
        return templates.TemplateResponse(
            request, "admin/migration.html", admin_ctx(request, staff, error=str(exc)), status_code=422
        )
    finally:
        os.unlink(path)

    # Bind: the import may only run against this exact file.
    request.session[_SESSION_HASH_KEY] = digest
    return templates.TemplateResponse(
        request, "admin/migration.html",
        admin_ctx(request, staff, report=report, filename=file.filename or "upload.csv",
                  file_rows=rows, file_short=digest[:12]),
    )


@router.post("/import", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
def run_import(
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    staff: StaffUser = Depends(require_staff),
):
    if confirm.strip() != "IMPORT":
        raise HTTPException(status_code=422, detail="Type IMPORT (in capitals) to confirm the write")

    reported = request.session.get(_SESSION_HASH_KEY)
    data, digest, rows = _read_hash_rows(file)
    if not reported:
        raise HTTPException(status_code=422, detail="Run the read-only report first, then import the same file")
    if digest != reported:
        raise HTTPException(
            status_code=422,
            detail="This file doesn't match the one you reported on (different hash) -- run the report again on the exact file you intend to import",
        )

    path = _to_tempfile(data)
    try:
        result = concierge_migration.import_migration_csv(db, path, venue=_venue(db), actor=f"staff:{staff.email}")
    except concierge_migration.MigrationInputError as exc:
        return templates.TemplateResponse(
            request, "admin/migration.html", admin_ctx(request, staff, error=str(exc)), status_code=422
        )
    finally:
        os.unlink(path)

    request.session.pop(_SESSION_HASH_KEY, None)  # one import per report
    return templates.TemplateResponse(
        request, "admin/migration.html",
        admin_ctx(request, staff, result=result, filename=file.filename or "upload.csv", file_short=digest[:12]),
    )
