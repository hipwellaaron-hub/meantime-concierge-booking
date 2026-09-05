"""One-off: export EVERY Concierge booking (all statuses) to CSV.

Read-only. Built to give a full picture of what's already in Concierge
before migrating the remaining iVvy bookings, so duplicates and
negotiated-terms risks can be spotted first. Not a permanent feature.

Usage (runs against whatever DATABASE_URL points at):
    python -m app.export_bookings_csv                 # -> bookings_export.csv
    python -m app.export_bookings_csv out.csv         # custom path

To export PRODUCTION data, run it with the production DATABASE_URL in the
environment (e.g. `railway run --service meantime-concierge-booking
python -m app.export_bookings_csv bookings_export.csv`). A plain local run
hits the dev database.

Notes on a few derived columns (see also the accompanying field notes):
- `space_min_food_spend` is the SPACE default; `agreed_min_food_spend` is what
  this booking actually agreed to and is the figure on its contract. `space_standard_min_adults` is included alongside
  `agreed_min_adults` so a negotiated-down minimum is visible at a glance
  (agreed < standard). The negotiated lever is the adult MINIMUM, not a
  dollar figure.
- `company` is not a first-class column: for imported bookings it comes from
  migration_snapshot; for enquiry/staff bookings it's parsed from a
  "Company: ..." line in notes if present.
- Money/`agreement`/`beo`/`wizard` reflect the CURRENT document/invoice/
  session state; superseded document versions are ignored.
"""

import csv
import re
import sys

from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Booking
from app.models.document import DocumentStatus, DocumentType  # noqa: F401 (kept for readers)
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.wizard_session import WizardSessionStatus

COLUMNS = [
    "booking_id", "reference", "event_name", "event_date",
    "start_time", "end_time", "proposed_time_slot",
    "space", "linked_spaces", "status", "event_type",
    "adults", "children",
    "agreed_min_adults", "agreed_min_reduction_reason",
    "space_standard_min_adults", "space_min_food_spend", "agreed_min_food_spend", "bar_credit",
    "contact_name", "contact_email", "contact_phone", "company",
    "agreement_status", "agreement_signed_at",
    "deposit_invoice_status", "deposit_amount", "deposit_paid_at",
    "wizard_status", "beo_status", "beo_version",
    "outside_cake_permitted", "vendors",
    "created_at", "source", "legacy_ref",
    "notes",
]

_COMPANY_RE = re.compile(r"Company:\s*(.+)")

_WIZARD_STATUS_LABEL = {
    WizardSessionStatus.pending: "not started",
    WizardSessionStatus.in_progress: "in progress",
    WizardSessionStatus.submitted: "submitted",
    WizardSessionStatus.revoked: "revoked",
}


def _iso(value):
    return value.isoformat() if value is not None else ""


def _current_document(booking, doc_type):
    for d in booking.documents:
        if d.type == doc_type and d.is_current:
            return d
    return None


def _deposit_invoice(booking):
    # Prefer a live (non-cancelled) deposit invoice; if several, the newest.
    deposits = [i for i in booking.invoices if i.type == InvoiceType.deposit]
    live = [i for i in deposits if i.status != InvoiceStatus.cancelled]
    pool = live or deposits
    if not pool:
        return None
    return sorted(pool, key=lambda i: i.created_at)[-1]


def _linked_space_names(booking):
    parent = booking.parent_booking or booking
    group = [parent] + list(parent.linked_bookings)
    names = [b.space.name for b in group if b.id != booking.id and b.space is not None]
    return "; ".join(names)


def _company(booking):
    snap = booking.migration_snapshot or {}
    if isinstance(snap, dict) and snap.get("company"):
        return snap["company"]
    if booking.notes:
        m = _COMPANY_RE.search(booking.notes)
        if m:
            return m.group(1).strip()
    return ""


def _source(booking):
    if booking.migration_source:
        return f"imported:{booking.migration_source}"
    if isinstance(booking.first_touch_attribution, dict):
        return "enquiry_form"
    return "staff_entered"


def _wizard_status(booking):
    ws = booking.wizard_session
    if ws is None:
        return "not started"
    return _WIZARD_STATUS_LABEL.get(ws.status, ws.status.value)


def _vendors(booking):
    parts = []
    for v in booking.vendors:
        confirmed = {True: "confirmed", False: "requested", None: ""}[v.bump_in_confirmed]
        parts.append(f"{v.vendor_type}:{v.name}" + (f" ({confirmed})" if confirmed else ""))
    return "; ".join(parts)


def row_for(booking) -> dict:
    agreement = _current_document(booking, DocumentType.agreement)
    beo = _current_document(booking, DocumentType.beo)
    deposit = _deposit_invoice(booking)
    contact = booking.contact
    return {
        "booking_id": str(booking.id),
        "reference": booking.reference_code,
        "event_name": booking.event_name,
        "event_date": _iso(booking.event_date),
        "start_time": _iso(booking.start_time),
        "end_time": _iso(booking.end_time),
        "proposed_time_slot": booking.proposed_time_slot or "",
        "space": booking.space.name if booking.space else "",
        "linked_spaces": _linked_space_names(booking),
        "status": booking.status.value,
        "event_type": booking.event_type or "",
        "adults": booking.adult_count,
        "children": booking.child_count,
        "agreed_min_adults": booking.agreed_min_adults,
        "agreed_min_reduction_reason": booking.agreed_min_reduction_reason.value
        if booking.agreed_min_reduction_reason else "",
        "space_standard_min_adults": booking.space.standard_min_adults if booking.space else "",
        "space_min_food_spend": str(booking.space.min_food_spend) if booking.space else "",
        "agreed_min_food_spend": str(booking.agreed_min_food_spend),
        "bar_credit": str(booking.bar_credit),
        "contact_name": contact.name if contact else "",
        "contact_email": contact.email if contact else "",
        "contact_phone": (contact.phone or "") if contact else "",
        "company": _company(booking),
        "agreement_status": agreement.status.value if agreement else "",
        "agreement_signed_at": _iso(agreement.signed_at) if agreement else "",
        "deposit_invoice_status": deposit.status.value if deposit else "",
        "deposit_amount": str(deposit.total) if deposit else "",
        "deposit_paid_at": _iso(deposit.paid_at) if deposit else "",
        "wizard_status": _wizard_status(booking),
        "beo_status": beo.status.value if beo else "",
        "beo_version": beo.version if beo else "",
        "outside_cake_permitted": "yes" if booking.outside_cake_permitted else "no",
        "vendors": _vendors(booking),
        "created_at": _iso(booking.created_at),
        "source": _source(booking),
        "legacy_ref": booking.migration_external_ref or "",
        "notes": (booking.notes or "").replace("\r\n", " / ").replace("\n", " / "),
    }


def export(path: str) -> int:
    db = SessionLocal()
    try:
        bookings = (
            db.query(Booking)
            .options(
                selectinload(Booking.space),
                selectinload(Booking.contact),
                selectinload(Booking.documents),
                selectinload(Booking.invoices),
                selectinload(Booking.wizard_session),
                selectinload(Booking.vendors),
                selectinload(Booking.linked_bookings).selectinload(Booking.space),
                selectinload(Booking.parent_booking).selectinload(Booking.linked_bookings).selectinload(Booking.space),
                selectinload(Booking.parent_booking).selectinload(Booking.space),
            )
            .order_by(Booking.event_date.is_(None), Booking.event_date, Booking.created_at)
            .all()
        )
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            for b in bookings:
                writer.writerow(row_for(b))
        return len(bookings)
    finally:
        db.close()


def main(argv: list[str]) -> None:
    path = argv[1] if len(argv) > 1 else "bookings_export.csv"
    count = export(path)
    print(f"Exported {count} booking(s) -> {path}")


if __name__ == "__main__":
    main(sys.argv)
