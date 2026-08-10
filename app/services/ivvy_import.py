"""iVvy CSV export importer.

iVvy does offer a CSV export (resolves an open question from the original
brief -- no manual-entry fallback path was needed). Two real limitations
of that export shape this importer, both deliberate rather than guessed
around:

- No room/space column. Imported bookings go into a placeholder
  "Unassigned (pending triage)" space (see app/seed.py) that never shows
  up in real availability listings (Space.is_bookable=False), and
  get_unassigned_bookings() below gives staff a worklist to assign the
  real space.
- No time-of-day, only a date. start_time/end_time are left NULL, same
  triage list surfaces these too.

Financial totals from iVvy (Total Amount, Total Paid, Total Outstanding)
are kept verbatim in migration_snapshot for reference -- they are NOT
turned into live Invoice/Payment records. Payments are the last, most
carefully staged part of the cutover for a reason; a bulk-imported invoice
with unreconciled GST/payment-processor assumptions baked in is exactly
the kind of silent-guess-with-real-money-consequences this build has
avoided everywhere else.

Idempotent: re-running against the same (or an overlapping) export is
safe -- rows are matched on migration_external_ref (iVvy's own Code) and
skipped if already imported.
"""

import csv
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Space, Venue
from app.models.booking import BookingStatus
from app.seed import UNASSIGNED_SPACE_NAME
from app.services.booking import create_booking
from app.services.contact_matching import find_or_create_contact
from app.utils import truncate

MIGRATION_SOURCE = "ivvy"

# Match the DB column widths (Booking.event_name / Contact.name) so a
# freak oversized field in an export can never crash the whole import --
# it gets truncated, not fatal.
EVENT_NAME_MAX_LENGTH = 255
CONTACT_NAME_MAX_LENGTH = 255

# Only the statuses actually seen in real exports are mapped. An
# unrecognized status fails loudly and is skipped rather than guessed at
# -- misrepresenting a real booking's state is worse than not importing
# the row at all.
STATUS_MAP = {
    "Confirmed": BookingStatus.confirmed,
}


@dataclass
class ImportRowError:
    row_number: int
    code: str
    reason: str


@dataclass
class ImportResult:
    created: int = 0
    skipped_existing: int = 0
    errors: list[ImportRowError] = field(default_factory=list)


def parse_date(value: str) -> dt.date:
    # iVvy format, e.g. "Friday, 24 July 2026"
    return dt.datetime.strptime(value.strip(), "%A, %d %B %Y").date()


def parse_money(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return str(Decimal(value.replace(",", "")))
    except InvalidOperation:
        return None


def get_unassigned_space_id(db: Session, venue: Venue):
    space = db.execute(
        select(Space.id).where(Space.venue_id == venue.id, Space.name == UNASSIGNED_SPACE_NAME)
    ).scalar_one()
    return space


def import_ivvy_csv(db: Session, csv_path: str, *, venue: Venue, actor: str = "ivvy_import") -> ImportResult:
    result = ImportResult()
    unassigned_space_id = get_unassigned_space_id(db, venue)

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, start=2):  # header is row 1
            if not any((v or "").strip() for v in row.values()):
                continue  # blank trailing row

            # A malformed row (missing/renamed column -- a future export
            # format change, a hand-edited CSV, anything) must not abort
            # the entire batch. One bad row becomes one reported error;
            # every other row still gets a chance to import.
            code = _get(row, "Code")
            try:
                _import_row(db, row, row_number, code, unassigned_space_id, actor, result)
            except (KeyError, AttributeError, TypeError) as exc:
                # A row shorter than the header (csv.DictReader fills
                # missing trailing fields with None, not a KeyError) or a
                # renamed/missing column must not abort the entire batch
                # -- one bad row becomes one reported error, every other
                # row still gets a chance to import.
                result.errors.append(ImportRowError(row_number, code, f"malformed row: {exc}"))
            except (ValueError, InvalidOperation) as exc:
                result.errors.append(ImportRowError(row_number, code, str(exc)))

    return result


def _get(row: dict, key: str) -> str:
    """Safe column access: handles both a missing/renamed column (dict
    lacks the key) and a row with fewer fields than the header
    (csv.DictReader fills those with None rather than raising)."""
    return (row.get(key) or "").strip()


def _import_row(db, row, row_number, code, unassigned_space_id, actor, result) -> None:
    if not code:
        result.errors.append(ImportRowError(row_number, code, "missing Code -- not imported"))
        return

    already_imported = db.execute(
        select(Booking.id).where(
            Booking.migration_source == MIGRATION_SOURCE,
            Booking.migration_external_ref == code,
        )
    ).first()
    if already_imported is not None:
        result.skipped_existing += 1
        return

    ivvy_status = _get(row, "Status")
    status = STATUS_MAP.get(ivvy_status)
    if status is None:
        result.errors.append(ImportRowError(row_number, code, f"unrecognized status '{ivvy_status}' -- not imported"))
        return

    start_date_str = _get(row, "Event Start Date")
    end_date_str = _get(row, "Event End Date")
    if start_date_str != end_date_str:
        result.errors.append(ImportRowError(row_number, code, "multi-day event -- not supported, not imported"))
        return

    try:
        event_date = parse_date(start_date_str)
    except ValueError as exc:
        result.errors.append(ImportRowError(row_number, code, f"could not parse date: {exc}"))
        return

    email = _get(row, "Email")
    name = _get(row, "Main Contact")
    if not email or not name:
        result.errors.append(ImportRowError(row_number, code, "missing contact name/email -- not imported"))
        return

    try:
        attendee_count = int(_get(row, "Total Attendees Guaranteed") or 0)
    except ValueError:
        attendee_count = 0

    # Every row access that could fail on a malformed row happens above
    # this line, deliberately, before any DB write for this row: a
    # malformed row must fail clean (nothing staged in the session) so it
    # can never leave a half-written orphan behind for a later row's
    # commit to accidentally sweep in.
    event_name = truncate(_get(row, "Booking Name") or f"Imported booking {code}", EVENT_NAME_MAX_LENGTH)

    contact, _duplicates = find_or_create_contact(db, truncate(name, CONTACT_NAME_MAX_LENGTH), email, None)

    snapshot = {
        "company": (row.get("Company") or "").strip() or None,
        "coordinator": (row.get("Coordinator") or "").strip() or None,
        "sales_person": (row.get("Sales Person") or "").strip() or None,
        "ivvy_type": (row.get("Type") or "").strip() or None,
        "total_amount": parse_money(row.get("Total Amount", "")),
        "total_tax_included": parse_money(row.get("Total Tax Included", "")),
        "total_paid": parse_money(row.get("Total Paid", "")),
        "total_outstanding": parse_money(row.get("Total Outstanding", "")),
        "last_modified": (row.get("Last Modified") or "").strip() or None,
        "ivvy_status": ivvy_status,
    }

    create_booking(
        db,
        space_id=unassigned_space_id,
        contact_id=contact.id,
        event_date=event_date,
        event_name=event_name,
        event_type=None,
        adult_count=attendee_count,
        child_count=0,
        notes=None,
        actor=actor,
        status=status,
        lead_source="ivvy_marketplace",
        migration_source=MIGRATION_SOURCE,
        migration_external_ref=code,
        migration_snapshot=snapshot,
    )
    result.created += 1


def get_unassigned_bookings(db: Session, venue: Venue) -> list[Booking]:
    """Staff worklist: bookings imported without a real space and/or real
    time assigned yet, oldest event first."""
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(Space.venue_id == venue.id, Space.is_bookable.is_(False))
            .order_by(Booking.event_date)
        ).all()
    )
