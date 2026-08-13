"""One-off, temporary: create the real bookings that
app/one_off_ivvy_reconcile_2026_08.py (already run and removed) could not --
those had no email in that export, so no Contact could be created
(Contact.email is NOT NULL). app/one_off_ivvy_create_new_2026_08.csv is a
merged export that fills in real emails for those Booking Codes, sourced
from iVvy's own opportunity record (email_source=ivvy_opportunity /
ivvy_opportunity_corrected) or cross-referenced against Gmail
(email_source=gmail) -- confirmed by Aaron as already verified.

Idempotent, same as every other importer here: skipped if a Booking with
this migration_source/migration_external_ref already exists (covers the
17 already backfilled and the 3 multi-space codes already imported under
one of their two spaces).

Multi-space Booking Codes (one event spanning two rooms) still aren't
representable in Concierge's one-space-per-booking model -- reported, not
created, same as the reconcile script's own limitation.

"Prospective" maps to BookingStatus.tentative (not "Confirmed"'s
.confirmed): a real space/date/time is already assigned in iVvy, so it
must sit behind the same double-booking exclusion constraint as a
confirmed booking or a staff-created hold (this is exactly how a hold is
already modelled elsewhere in this app -- see app/services/booking.py's
HOLD_FULL_DAY_START comment). Any other status is unrecognized and
skipped, not guessed at.
"""

import csv
import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import Space, Venue
from app.models.booking import Booking, BookingStatus
from app.services.booking import create_booking
from app.services.contact_matching import find_or_create_contact

CSV_PATH = "app/one_off_ivvy_create_new_2026_08.csv"
MIGRATION_SOURCE = "ivvy"

STATUS_MAP = {
    "Confirmed": BookingStatus.confirmed,
    "Prospective": BookingStatus.tentative,
}


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value.strip(), "%d/%m/%Y").date()


def parse_time(value: str) -> dt.time:
    return dt.datetime.strptime(value.strip().upper(), "%I:%M %p").time()


def load_rows():
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def group_by_code(rows):
    grouped = defaultdict(list)
    for row in rows:
        code = (row.get("reference_code") or "").strip()
        if not code:
            continue  # blockout / non-client rows have no code -- not a real booking
        grouped[code].append(row)
    return grouped


def pick_primary_row(rows):
    with_pax = [r for r in rows if (r.get("pax") or "").strip()]
    return with_pax[0] if with_pax else rows[0]


def already_imported(db, code) -> bool:
    return (
        db.execute(
            select(Booking.id).where(
                Booking.migration_source == MIGRATION_SOURCE, Booking.migration_external_ref == code
            )
        ).first()
        is not None
    )


def main():
    db = SessionLocal()
    venue = db.query(Venue).filter_by(slug="hamilton").one()
    space_by_name = {s.name: s for s in db.query(Space).filter_by(venue_id=venue.id).all()}

    rows = load_rows()
    grouped = group_by_code(rows)

    created = []
    skipped_existing = []
    multi_space_codes = []
    no_email = []
    unrecognized_status = []
    parse_errors = []
    conflicts = []

    for code, code_rows in grouped.items():
        if already_imported(db, code):
            skipped_existing.append(code)
            continue

        if len(code_rows) > 1:
            spaces_seen = {r["space"].strip() for r in code_rows}
            if len(spaces_seen) > 1:
                multi_space_codes.append((code, code_rows[0]["event_name"], sorted(spaces_seen)))
                continue

        row = pick_primary_row(code_rows)
        event_name = row["event_name"].strip()
        space_name = row["space"].strip()
        status_str = row["status"].strip()
        email = (row.get("contact_email") or "").strip()
        name = (row.get("contact_name") or "").strip()
        phone = (row.get("contact_phone_normalised") or "").strip() or None

        if not email or not name:
            no_email.append((code, event_name, row.get("flags", "")))
            continue

        status = STATUS_MAP.get(status_str)
        if status is None:
            unrecognized_status.append((code, event_name, status_str))
            continue

        try:
            event_date = parse_date(row["event_date"])
            start_time = parse_time(row["start_time"])
            end_time = parse_time(row["end_time"])
        except ValueError as exc:
            parse_errors.append((code, event_name, str(exc)))
            continue

        space = space_by_name.get(space_name)
        if space is None:
            parse_errors.append((code, event_name, f"unknown space '{space_name}'"))
            continue

        pax_str = (row.get("pax") or "").strip()
        adult_count = int(pax_str) if pax_str.isdigit() else 0

        contact, _dupes = find_or_create_contact(db, name, email, phone)

        snapshot = {
            "company": (row.get("company_name") or "").strip() or None,
            "opportunity_stage": (row.get("opportunity_stage") or "").strip() or None,
            "opportunity_source": (row.get("opportunity_source") or "").strip() or None,
            "beo_number": (row.get("beo_number") or "").strip() or None,
            "comments": (row.get("comments") or "").strip() or None,
            "layout": (row.get("layout") or "").strip() or None,
            "total_revenue": (row.get("total_revenue") or "").strip() or None,
            "total_paid": (row.get("total_paid") or "").strip() or None,
            "total_outstanding": (row.get("total_outstanding") or "").strip() or None,
            "created_date": (row.get("created_date") or "").strip() or None,
            "email_source": (row.get("email_source") or "").strip() or None,
            "flags": (row.get("flags") or "").strip() or None,
            "ivvy_status": status_str,
        }

        try:
            create_booking(
                db,
                space_id=space.id,
                contact_id=contact.id,
                event_date=event_date,
                start_time=start_time,
                end_time=end_time,
                event_name=event_name,
                event_type=(row.get("event_type") or "").strip() or None,
                adult_count=adult_count,
                child_count=0,
                notes=None,
                actor="ivvy_create_new_2026_08",
                status=status,
                lead_source="ivvy_marketplace",
                migration_source=MIGRATION_SOURCE,
                migration_external_ref=code,
                migration_snapshot=snapshot,
            )
        except IntegrityError as exc:
            db.rollback()
            conflicts.append((code, event_name, space_name, event_date.isoformat(), str(start_time), str(end_time), str(exc.orig)))
            continue

        created.append((code, event_name, space_name, event_date.isoformat(), str(start_time), str(end_time), status_str))

    print(f"=== Created ({len(created)}) ===")
    for code, name, space_name, date, start, end, status_str in created:
        print(f"  {code}  {name!r}  -> {space_name} on {date} {start}-{end}  status={status_str}")

    print(f"\n=== Already imported -- skipped ({len(skipped_existing)}) ===")
    for code in skipped_existing:
        print(f"  {code}")

    print(f"\n=== Multi-space Booking Codes -- not representable, not created ({len(multi_space_codes)}) ===")
    for code, name, spaces in multi_space_codes:
        print(f"  {code}  {name!r}  spans: {', '.join(spaces)}")

    print(f"\n=== No email -- not created ({len(no_email)}) ===")
    for code, name, flags in no_email:
        print(f"  {code}  {name!r}  flags={flags}")

    print(f"\n=== Unrecognized status -- not created ({len(unrecognized_status)}) ===")
    for code, name, status_str in unrecognized_status:
        print(f"  {code}  {name!r}  status={status_str!r}")

    print(f"\n=== Rows that failed to parse ({len(parse_errors)}) ===")
    for code, name, reason in parse_errors:
        print(f"  {code}  {name!r}: {reason}")

    print(f"\n=== Conflicts -- space/time already occupied, NOT created ({len(conflicts)}) ===")
    for code, name, space_name, date, start, end, reason in conflicts:
        print(f"  {code}  {name!r}  wanted: {space_name} on {date} {start}-{end}")
        print(f"      {reason}")

    db.close()


if __name__ == "__main__":
    main()
