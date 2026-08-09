"""Parallel-run reconciliation.

Compares a fresh iVvy export against what Concierge already has (matched
on migration_external_ref, iVvy's own booking code) and reports where they
diverge. It never writes anything -- a human decides what to do with a
divergence, the same "surface, don't auto-merge/auto-correct" stance used
everywhere else data quality is uncertain.

Per the build brief: do not cancel iVvy until this report runs clean for
a defined period.
"""

import csv
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Space, Venue
from app.services.ivvy_import import MIGRATION_SOURCE, STATUS_MAP, _get, parse_money


@dataclass
class Divergence:
    code: str
    field: str
    concierge_value: str | None
    ivvy_value: str | None


@dataclass
class ReconciliationReport:
    new_in_ivvy: list[str] = field(default_factory=list)
    missing_from_export: list[str] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    # Rows that couldn't be compared at all (e.g. a missing/renamed CSV
    # column) -- distinct from a divergence, since we genuinely don't know
    # whether that row matches or not.
    row_errors: list[str] = field(default_factory=list)
    matched_clean: int = 0

    @property
    def is_clean(self) -> bool:
        # missing_from_export is informational only -- it's just as likely
        # to mean "outside this export's date range" as "deleted in iVvy",
        # so it doesn't by itself count against a clean run. A row error
        # does count: an uncompared row is not a verified-clean row.
        return not self.new_in_ivvy and not self.divergences and not self.row_errors


def reconcile(db: Session, csv_path: str, *, venue: Venue) -> ReconciliationReport:
    report = ReconciliationReport()

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]

    seen_codes = set()
    for row_number, row in enumerate(rows, start=2):  # header is row 1
        code = _get(row, "Code")
        if code:
            seen_codes.add(code)
        try:
            _reconcile_row(db, row, code, report)
        except (KeyError, AttributeError, TypeError) as exc:
            # Same reasoning as ivvy_import.py: a row shorter than the
            # header yields None (not a KeyError) for missing trailing
            # fields, and that must not abort the whole report either.
            report.row_errors.append(f"row {row_number} ({code or 'no code'}): malformed row: {exc}")

    imported_codes = set(
        db.execute(
            select(Booking.migration_external_ref)
            .join(Space, Booking.space_id == Space.id)
            .where(Booking.migration_source == MIGRATION_SOURCE, Space.venue_id == venue.id)
        )
        .scalars()
        .all()
    )
    report.missing_from_export = sorted(imported_codes - seen_codes)

    return report


def _reconcile_row(db: Session, row: dict, code: str, report: ReconciliationReport) -> None:
    if not code:
        report.row_errors.append("row has no Code -- cannot match against Concierge")
        return

    booking = db.execute(
        select(Booking).where(
            Booking.migration_source == MIGRATION_SOURCE,
            Booking.migration_external_ref == code,
        )
    ).scalar_one_or_none()

    if booking is None:
        report.new_in_ivvy.append(code)
        return

    row_divergences: list[Divergence] = []

    ivvy_status = _get(row, "Status")
    expected_status = STATUS_MAP.get(ivvy_status)
    if expected_status is None:
        # Includes e.g. "Cancelled" -- a status we've never seen mapped
        # is exactly the case a reconciliation report exists to catch,
        # not silently skip.
        row_divergences.append(Divergence(code, "status", booking.status.value, f"unrecognized iVvy status: {ivvy_status}"))
    elif booking.status.value != expected_status.value:
        row_divergences.append(Divergence(code, "status", booking.status.value, expected_status.value))

    try:
        ivvy_attendees = int(_get(row, "Total Attendees Guaranteed") or 0)
        if booking.adult_count != ivvy_attendees:
            row_divergences.append(Divergence(code, "attendee_count", str(booking.adult_count), str(ivvy_attendees)))
    except ValueError:
        pass

    snapshot = booking.migration_snapshot or {}
    for field_name, csv_column in (("total_paid", "Total Paid"), ("total_outstanding", "Total Outstanding")):
        ivvy_value = parse_money(row.get(csv_column, ""))
        stored_value = snapshot.get(field_name)
        if ivvy_value != stored_value:
            row_divergences.append(Divergence(code, field_name, stored_value, ivvy_value))

    if row_divergences:
        report.divergences.extend(row_divergences)
    else:
        report.matched_clean += 1
