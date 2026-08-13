"""One-off, temporary: reconcile app/one_off_ivvy_reconcile_2026_08.csv
(a newer iVvy export -- Date/Space/Start Time/End Time/Pax columns, which
the original bulk importer's format never had) against what's already in
Concierge.

Two clearly-safe actions only:
  1. BACKFILL: for an already-imported booking (migration_source='ivvy',
     migration_external_ref = this CSV's Booking Code) still sitting in
     the Unassigned placeholder space, assign its real space + start/end
     time from this export. Additive only -- never touches a booking
     that already has a real space assigned.
  2. REPORT (no writes): anything that needs a human decision --
     Booking Codes with no match in Concierge at all (candidates to
     create, but this export has no email column and Concierge's contact
     matching is email-keyed, so these are NOT auto-created), and
     already-triaged bookings whose CSV data now disagrees with what's
     stored (pax/date/time changed since import -- status changes are
     never auto-applied either, since a booking may have been
     legitimately progressed in Concierge past whatever this export
     still shows).

Multi-space Booking Codes (a whole-venue hire spanning two rooms) don't
fit Concierge's one-booking-one-space model -- see the report section.
This file and its companion CSV are temporary, run once via
preDeployCommand, then removed.
"""

import csv
import datetime as dt
from collections import defaultdict

from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import Booking, Space, Venue
from app.models.booking import BookingStatus
from app.services.booking import assign_space_and_time

CSV_PATH = "app/one_off_ivvy_reconcile_2026_08.csv"
MIGRATION_SOURCE = "ivvy"


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
        code = (row.get("Booking Code") or "").strip()
        if not code:
            continue  # blockout / non-client rows have no code -- not a real booking
        grouped[code].append(row)
    return grouped


def pick_primary_row(rows):
    """For a multi-space Booking Code, the row carrying a real Pax count
    is the one representing the actual booking; the companion row (blank
    Pax) is the same event spilling into a second room, which Concierge's
    one-space-per-booking model has no way to represent -- reported, not
    silently merged or dropped."""
    with_pax = [r for r in rows if (r.get("Pax") or "").strip()]
    return with_pax[0] if with_pax else rows[0]


def main():
    db = SessionLocal()
    venue = db.query(Venue).filter_by(slug="hamilton").one()
    space_by_name = {s.name: s for s in db.query(Space).filter_by(venue_id=venue.id).all()}
    unassigned_space_id = space_by_name["Unassigned (pending triage)"].id

    rows = load_rows()
    grouped = group_by_code(rows)

    backfilled = []
    multi_space_codes = []
    no_match = []
    discrepancies = []
    parse_errors = []
    conflicts = []

    for code, code_rows in grouped.items():
        if len(code_rows) > 1:
            spaces_seen = {r["Space"].strip() for r in code_rows}
            if len(spaces_seen) > 1:
                multi_space_codes.append((code, code_rows[0]["Booking Name"], sorted(spaces_seen)))

        row = pick_primary_row(code_rows)
        event_name = row["Booking Name"].strip()
        space_name = row["Space"].strip()
        status_str = row["Status"].strip()

        try:
            event_date = parse_date(row["Date"])
            start_time = parse_time(row["Start Time"])
            end_time = parse_time(row["End Time"])
        except ValueError as exc:
            parse_errors.append((code, event_name, str(exc)))
            continue

        pax_str = (row.get("Pax") or "").strip()
        pax = int(pax_str) if pax_str.isdigit() else None

        space = space_by_name.get(space_name)
        if space is None:
            parse_errors.append((code, event_name, f"unknown space '{space_name}'"))
            continue

        existing = (
            db.query(Booking)
            .filter(Booking.migration_source == MIGRATION_SOURCE, Booking.migration_external_ref == code)
            .one_or_none()
        )

        if existing is None:
            no_match.append((code, event_name, event_date.isoformat(), space_name, pax, status_str, row["Contact Name"], row["Contact Phone"]))
            continue

        if existing.space_id == unassigned_space_id:
            try:
                assign_space_and_time(
                    db, existing, space_id=space.id, start_time=start_time, end_time=end_time,
                    event_date=event_date, actor="ivvy_reconcile_2026_08",
                )
            except IntegrityError as exc:
                db.rollback()
                conflicts.append((code, event_name, space_name, event_date.isoformat(), str(start_time), str(end_time), str(exc.orig)))
                continue
            backfilled.append((code, event_name, space_name, event_date.isoformat(), str(start_time), str(end_time)))
        else:
            diffs = []
            if existing.event_date != event_date:
                diffs.append(f"date: concierge={existing.event_date} csv={event_date}")
            if existing.start_time != start_time or existing.end_time != end_time:
                diffs.append(f"time: concierge={existing.start_time}-{existing.end_time} csv={start_time}-{end_time}")
            if existing.space.name != space_name:
                diffs.append(f"space: concierge={existing.space.name} csv={space_name}")
            if pax is not None and existing.adult_count != pax:
                diffs.append(f"pax: concierge={existing.adult_count} csv={pax}")
            if diffs:
                discrepancies.append((code, event_name, diffs))

    print(f"=== Backfilled ({len(backfilled)}) ===")
    for code, name, space_name, date, start, end in backfilled:
        print(f"  {code}  {name!r}  -> {space_name} on {date} {start}-{end}")

    print(f"\n=== Multi-space Booking Codes -- not representable, not touched ({len(multi_space_codes)}) ===")
    for code, name, spaces in multi_space_codes:
        print(f"  {code}  {name!r}  spans: {', '.join(spaces)}")

    print(f"\n=== No match in Concierge -- NOT created (no email in this export) ({len(no_match)}) ===")
    for code, name, date, space_name, pax, status, contact, phone in no_match:
        print(f"  {code}  {name!r}  {date} {space_name} pax={pax} status={status}  contact={contact} phone={phone}")

    print(f"\n=== Already-triaged bookings with data that now disagrees -- NOT auto-overwritten ({len(discrepancies)}) ===")
    for code, name, diffs in discrepancies:
        print(f"  {code}  {name!r}")
        for d in diffs:
            print(f"      {d}")

    print(f"\n=== Rows that failed to parse ({len(parse_errors)}) ===")
    for code, name, reason in parse_errors:
        print(f"  {code}  {name!r}: {reason}")

    print(f"\n=== Backfill conflicts -- space/time already occupied by another booking, NOT applied ({len(conflicts)}) ===")
    for code, name, space_name, date, start, end, reason in conflicts:
        print(f"  {code}  {name!r}  wanted: {space_name} on {date} {start}-{end}")
        print(f"      {reason}")

    db.close()


if __name__ == "__main__":
    main()
