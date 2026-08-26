"""Importer for iVvy's "ag-grid" calendar/session export -- a genuinely
different report shape from the one app.services.ivvy_import already
handles (that one is iVvy's structured Bookings CSV export). This export
comes from iVvy's own venue calendar view and has real Space and
Start/End Time columns the other export never had, but is missing
anything to build a Contact from: no email column at all.

Two real consequences of that:

- Every booking created here has contact_id=None. Contact.email is
  NOT NULL in this schema, so a Contact literally cannot be created
  without one -- there is nothing to guess, so nothing is guessed.
  Whatever name/phone/company iVvy did capture goes into the booking's
  own notes instead, verbatim, so staff still see it.
- No document, invoice, or wizard link can be sent for any of these
  bookings until a staff member adds a real contact with a real email --
  every one of those send paths already refuses without one (see
  app.services.documents.mark_sent and friends). That's the existing
  guard doing exactly its job, not a new gap.

Because real Space and time ARE known here, bookings go straight into
their real space at `confirmed` status -- genuinely double-booking-
protected immediately, unlike app.services.ivvy_import's Unassigned-
placeholder default (that export never had a real space to assign).

A booking code appearing more than once (a real event using two rooms at
once) is modelled the same way anywhere else in this app models it: one
parent booking plus a linked child space (see
app.services.booking.add_linked_space), not two independent bookings
that happen to share a name.

Idempotent against *both* importers: matched on migration_external_ref
(iVvy's own Booking Code) alone, regardless of migration_source. The
Booking Code is iVvy's own globally unique identifier -- a booking
already pulled in by app.services.ivvy_import (migration_source="ivvy")
under the same code is the same real booking, not a new one, even though
this export's own rows are tagged "ivvy_calendar" for their own
provenance. Confirmed this matters in practice, not just in theory: a
real dry run against data mirroring production found this exact
booking-already-exists-under-the-other-importer's-tag case for the
large majority of rows in a real export.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Space, Venue
from app.models.booking import BookingStatus
from app.services.booking import add_linked_space, create_booking

MIGRATION_SOURCE = "ivvy_calendar"

EVENT_NAME_MAX_LENGTH = 255

# Only a status actually seen in a real export is mapped -- an
# unrecognized one fails loudly and is skipped rather than guessed at,
# same reasoning as app.services.ivvy_import.STATUS_MAP.
STATUS_MAP = {
    "Confirmed": BookingStatus.confirmed,
}


@dataclass
class ImportRowError:
    booking_code: str | None
    reason: str


@dataclass
class ImportResult:
    created: int = 0
    linked_spaces_added: int = 0
    skipped_existing: int = 0
    errors: list[ImportRowError] = field(default_factory=list)
    created_reference_codes: list[str] = field(default_factory=list)


def parse_export_date(value: str) -> dt.date:
    """The export's own format, e.g. "24/10/2026" -- day/month/year, not
    iVvy's other export's "Friday, 24 July 2026"."""
    return dt.datetime.strptime(value.strip(), "%d/%m/%Y").date()


def parse_export_time(value: str) -> dt.time:
    """e.g. "6:00 pm"."""
    return dt.datetime.strptime(value.strip().lower(), "%I:%M %p").time()


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_pax(value) -> int:
    try:
        return int(_clean(value) or 0)
    except ValueError:
        return 0


def _build_notes(row: dict) -> str | None:
    lines = []
    contact_name = _clean(row.get("Contact Name"))
    contact_phone = _clean(row.get("Contact Phone"))
    company = _clean(row.get("Company Name"))
    booked_by = _clean(row.get("Booked By"))
    comments = _clean(row.get("Comments"))
    beo_number = _clean(row.get("BEO#"))

    if contact_name:
        lines.append(f"Contact: {contact_name}" + (f" ({contact_phone})" if contact_phone else ""))
    elif contact_phone:
        lines.append(f"Contact phone: {contact_phone}")
    if company:
        lines.append(f"Company: {company}")
    if booked_by:
        lines.append(f"Booked by (iVvy): {booked_by}")
    if beo_number:
        lines.append(f"BEO#: {beo_number}")
    if comments:
        lines.append(comments)
    lines.append("Imported from iVvy calendar export -- no email captured; add a real contact before sending anything to this client.")
    return "\n".join(lines)


def _build_snapshot(row: dict) -> dict:
    def money(key: str) -> str | None:
        raw = _clean(row.get(key))
        if raw is None:
            return None
        try:
            return str(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            return None

    return {
        "session_name": _clean(row.get("Session Name")),
        "venue": _clean(row.get("Venue")),
        "package": _clean(row.get("Package")),
        "layout": _clean(row.get("Layout")),
        "setup_time": _clean(row.get("Setup Time")),
        "pack_down_time": _clean(row.get("Pack Down Time")),
        "amount": money("Amount"),
        "total_amount": money("Total Amount"),
        "service_fees": money("Service Fees"),
    }


def _get_bookable_space(db: Session, venue: Venue, space_name: str) -> Space | None:
    return db.execute(
        select(Space).where(Space.venue_id == venue.id, Space.name == space_name, Space.is_bookable.is_(True))
    ).scalar_one_or_none()


def import_calendar_rows(db: Session, rows: list[dict], *, venue: Venue, actor: str = "ivvy_calendar_import") -> ImportResult:
    result = ImportResult()

    # Group by Booking Code first so a code appearing twice (one real
    # event in two rooms at once) is handled as one parent + one linked
    # child, never as two unrelated bookings.
    groups: dict[str, list[dict]] = {}
    for row in rows:
        code = _clean(row.get("Booking Code"))
        if not code:
            continue  # a genuinely blank placeholder row, e.g. a blocked-out day -- not a real booking
        groups.setdefault(code, []).append(row)

    for code, group_rows in groups.items():
        try:
            _import_group(db, code, group_rows, venue, actor, result)
        except Exception as exc:  # noqa: BLE001 -- one bad group (a real double-booking
            # conflict, a space name that doesn't match any real space, a malformed
            # date/time) must not abort every other group in the same batch.
            db.rollback()
            result.errors.append(ImportRowError(code, str(exc)))

    return result


def _import_group(db: Session, code: str, group_rows: list[dict], venue: Venue, actor: str, result: ImportResult) -> None:
    # Matched on the code alone, not also migration_source -- see the
    # module docstring on why a booking already imported by the *other*
    # iVvy importer must still count as "already imported" here.
    already_imported = db.execute(select(Booking.id).where(Booking.migration_external_ref == code)).first()
    if already_imported is not None:
        result.skipped_existing += 1
        return

    # The row with the most guests becomes the parent -- for the one real
    # multi-space case in this export, the "second room" row often has no
    # Pax recorded at all (the guest count is for the whole event, not
    # split per room).
    group_rows = sorted(group_rows, key=lambda r: _parse_pax(r.get("Pax")), reverse=True)
    primary, *extra_rows = group_rows

    status = STATUS_MAP.get(_clean(primary.get("Status")) or "")
    if status is None:
        result.errors.append(ImportRowError(code, f"unrecognized status '{primary.get('Status')}' -- not imported"))
        return

    event_date = parse_export_date(primary["Date"])
    start_time = parse_export_time(primary["Start Time"])
    end_time = parse_export_time(primary["End Time"])

    space_name = _clean(primary.get("Space"))
    space = _get_bookable_space(db, venue, space_name) if space_name else None
    if space is None:
        result.errors.append(ImportRowError(code, f"no real bookable space named '{space_name}' -- not imported"))
        return

    event_name = _clean(primary.get("Booking Name")) or f"Imported booking {code}"
    event_name = event_name[:EVENT_NAME_MAX_LENGTH]

    booking = create_booking(
        db,
        space_id=space.id,
        contact_id=None,
        event_date=event_date,
        start_time=start_time,
        end_time=end_time,
        event_name=event_name,
        event_type=None,
        adult_count=_parse_pax(primary.get("Pax")),
        child_count=0,
        notes=_build_notes(primary),
        actor=actor,
        status=status,
        lead_source="ivvy_marketplace",
        migration_source=MIGRATION_SOURCE,
        migration_external_ref=code,
        migration_snapshot=_build_snapshot(primary),
    )
    result.created += 1
    result.created_reference_codes.append(booking.reference_code)

    for extra in extra_rows:
        extra_space_name = _clean(extra.get("Space"))
        extra_space = _get_bookable_space(db, venue, extra_space_name) if extra_space_name else None
        if extra_space is None:
            result.errors.append(
                ImportRowError(code, f"linked space '{extra_space_name}' not found -- parent booking still created")
            )
            continue
        extra_start = parse_export_time(extra["Start Time"]) if _clean(extra.get("Start Time")) else start_time
        extra_end = parse_export_time(extra["End Time"]) if _clean(extra.get("End Time")) else end_time
        add_linked_space(db, booking, space_id=extra_space.id, start_time=extra_start, end_time=extra_end, actor=actor)
        result.linked_spaces_added += 1
