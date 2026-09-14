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

from decimal import Decimal, InvalidOperation

from app.models import Booking, Space, Venue
from app.models.invoice import InvoiceStatus
from app.services import invoicing
from app.services.ivvy_import import MIGRATION_SOURCE, STATUS_MAP, _get


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



def _money_or_none(raw: str) -> Decimal | None:
    """iVvy's figure as a NUMBER, or None when it genuinely cannot be read.

    Deliberately not ivvy_import.parse_money, which returns a string and
    therefore compares by formatting. Tolerates the shapes an export
    actually carries -- currency symbols, thousands separators, parentheses
    for negatives -- because a column that reads '$1,450.00' is a readable
    figure, and calling it unreadable would report a divergence on a row
    that agrees.
    """
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _live_money(db: Session, booking) -> tuple[Decimal, Decimal]:
    """What CONCIERGE holds for this booking right now: total received, and
    total still outstanding.

    Read from the invoices and payments tables, never from
    migration_snapshot -- the snapshot is iVvy's own figure frozen at
    import, so comparing it to iVvy compares iVvy with itself.

    Cancelled and legacy-superseded invoices are excluded from OUTSTANDING
    (nobody owes them) but their payments still count towards PAID, which
    is the same rule invoicing.get_deposit_paid states and for the same
    reason: the client handed the money over.
    """
    paid = Decimal("0.00")
    outstanding = Decimal("0.00")
    for invoice in booking.invoices:
        invoice_paid = invoicing.get_total_paid(db, invoice.id)
        paid += invoice_paid
        if invoice.status != InvoiceStatus.cancelled:
            remaining = invoice.total - invoice_paid
            if remaining > 0:
                outstanding += remaining
    return paid, outstanding


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

    # MONEY IS COMPARED AS NUMBERS, AGAINST LIVE RECORDS. Three faults
    # lived in the six lines this replaces, and the module's whole purpose
    # -- "do not cancel iVvy until this report runs clean" -- rested on
    # them.
    #
    #   * It compared STRINGS. parse_money returned str(Decimal(v)), which
    #     preserves whatever formatting its source used, so '500' != '500.00'
    #     reported a divergence between two identical amounts. The August
    #     import stored 2dp strings while a raw Bookings export carries
    #     unpadded ones, so a re-run could diverge on every row for no
    #     reason.
    #   * An unparseable figure became None, and a stored None compared
    #     EQUAL to it -- so '$500.00' on both sides counted as MATCHED
    #     CLEAN. The failure mode was silence, in the direction of saying
    #     everything is fine.
    #   * It compared iVvy against migration_snapshot, which is iVvy's own
    #     figure frozen at import. That can only detect iVvy changing since
    #     the import. It could never see Concierge's live money drifting
    #     from iVvy's -- which is the one thing a parallel run exists to
    #     catch before the old system is switched off.
    live_paid, live_outstanding = _live_money(db, booking)
    for field_name, csv_column, live_value in (
        ("total_paid", "Total Paid", live_paid),
        ("total_outstanding", "Total Outstanding", live_outstanding),
    ):
        raw = (row.get(csv_column) or "").strip()
        ivvy_value = _money_or_none(raw)
        if ivvy_value is None:
            if raw:
                # A figure that cannot be read is a divergence, not a match.
                row_divergences.append(
                    Divergence(code, field_name, str(live_value), f"unreadable: {raw!r}")
                )
            continue
        if ivvy_value != live_value:
            row_divergences.append(
                Divergence(code, field_name, str(live_value), str(ivvy_value))
            )

    if row_divergences:
        report.divergences.extend(row_divergences)
    else:
        report.matched_clean += 1
