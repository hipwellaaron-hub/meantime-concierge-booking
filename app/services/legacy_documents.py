"""Legacy document/invoice uploads for the iVvy migration.

A legacy document is an inert record of what a client already signed / paid
in iVvy. The migration import creates the record (a signed agreement
Document, a paid deposit Invoice+Payment) so the booking's gates are
satisfied from data; this module lets staff attach the original signed PDF
to that record afterwards -- or create the record directly from an upload
where the import didn't.

Everything here treats the PDF as OPAQUE bytes: validated only by size and a
%PDF magic header, stored as-is, never parsed or rendered server-side, and
served only as an attachment. Legacy rows are read-only elsewhere (see the
is_legacy guards in app.services.documents / app.services.invoicing and the
client-route guards in app.api.documents).
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Booking
from app.models.booking_event import BookingEvent
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.payment import Payment, PaymentMethod
from app.services import documents as documents_service

MAX_PDF_BYTES = 15 * 1024 * 1024  # generous for a scanned multi-page agreement; a hard ceiling all the same
_PDF_MAGIC = b"%PDF-"


class LegacyUploadError(ValueError):
    """A bad upload (not a PDF, too big, wrong booking state). Surfaced to
    staff as a 422; never a 500."""


def validate_pdf(data: bytes) -> None:
    if not data:
        raise LegacyUploadError("the uploaded file is empty")
    if len(data) > MAX_PDF_BYTES:
        raise LegacyUploadError(f"file is too large (max {MAX_PDF_BYTES // (1024 * 1024)} MB)")
    if not data.startswith(_PDF_MAGIC):
        raise LegacyUploadError("that doesn't look like a PDF (missing %PDF header)")


def booking_snapshot(booking: Booking) -> dict:
    """The booking facts the stored PDF represents, captured so a later
    change to the booking can be flagged rather than silently diverging."""
    return {
        "event_date": booking.event_date.isoformat() if booking.event_date else None,
        "space": booking.space.name if booking.space else None,
    }


def snapshot_mismatch(row, booking: Booking) -> str | None:
    """Human-readable description of how the live booking has drifted from
    what the legacy PDF says, or None if they still match."""
    snap = row.legacy_snapshot or {}
    current = booking_snapshot(booking)
    diffs = []
    if snap.get("event_date") != current["event_date"]:
        diffs.append(f"date was {snap.get('event_date') or '—'}, now {current['event_date'] or '—'}")
    if snap.get("space") != current["space"]:
        diffs.append(f"space was {snap.get('space') or '—'}, now {current['space'] or '—'}")
    return "; ".join(diffs) or None


def legacy_mismatches(booking: Booking) -> list[tuple[str, str]]:
    """(label, mismatch) for every legacy row on this booking whose snapshot
    no longer matches -- what the admin Documents section warns on."""
    out = []
    for d in booking.documents:
        if d.is_legacy:
            m = snapshot_mismatch(d, booking)
            if m:
                out.append((f"legacy {d.type.value}", m))
    for inv in booking.invoices:
        if inv.is_legacy:
            m = snapshot_mismatch(inv, booking)
            if m:
                out.append((f"legacy {inv.type.value} invoice", m))
    return out


def _legacy_content(source_ref: str | None) -> dict:
    return {
        "legacy": True,
        "source": "ivvy",
        "source_ref": source_ref,
        "note": "Original agreement signed in iVvy; the signed PDF is the record. "
        "This placeholder is never rendered to a client.",
    }


def mark_agreement_legacy(
    doc: Document, *, source_ref: str | None, booking: Booking, signed_at: dt.datetime | None = None
) -> None:
    """Stamp an already-created signed agreement Document as a legacy record
    (used by the migration importer, which builds the Document itself)."""
    doc.is_legacy = True
    doc.legacy_source_ref = source_ref
    doc.legacy_snapshot = booking_snapshot(booking)
    if signed_at is not None:
        doc.signed_at = signed_at


def attach_agreement_pdf(
    db: Session, booking: Booking, *, pdf: bytes, filename: str,
    source_ref: str | None = None, signed_at: dt.datetime | None = None, actor: str,
) -> Document:
    """Attach a signed agreement PDF to a booking. Attaches to the existing
    legacy agreement record if there is one (the migration path), otherwise
    creates a fresh inert signed agreement (which also satisfies the
    has-signed-agreement gate). Refuses if a LIVE generated agreement already
    holds the current slot -- that's a real document, not to be shadowed."""
    if booking.parent_booking_id is not None:
        raise LegacyUploadError("attach the agreement to the parent booking, not a linked room")
    validate_pdf(pdf)

    current = documents_service.get_current(db, booking.id, DocumentType.agreement)
    if current is not None and not current.is_legacy:
        raise LegacyUploadError(
            "a live generated agreement already exists for this booking -- a legacy PDF would shadow it"
        )

    if current is None:
        current = documents_service.create_new_version(
            db, booking, DocumentType.agreement, _legacy_content(source_ref), actor=actor
        )
        current.status = DocumentStatus.signed
        current.signer_name = "Migrated from iVvy"

    current.is_legacy = True
    current.signed_at = signed_at or current.signed_at or dt.datetime.now(dt.timezone.utc)
    current.legacy_file = pdf
    current.legacy_filename = filename
    if source_ref:
        current.legacy_source_ref = source_ref
    current.legacy_snapshot = booking_snapshot(booking)

    db.add(BookingEvent(
        booking_id=booking.id, event_type="legacy_agreement_uploaded",
        field_name="agreement", new_value=(source_ref or filename), actor=actor,
    ))
    db.commit()
    db.refresh(current)
    return current


def attach_deposit_pdf(
    db: Session, booking: Booking, *, pdf: bytes, filename: str, amount: Decimal,
    paid_at: dt.datetime | None = None, source_ref: str | None = None, actor: str,
) -> Invoice:
    """Attach a paid-deposit PDF. Attaches to the existing legacy deposit
    Invoice if present; otherwise creates an inert paid deposit Invoice +
    Payment (so the deposit gate is satisfied and a later final invoice
    credits it exactly once via get_deposit_paid). Refuses if a LIVE deposit
    invoice already exists."""
    if booking.parent_booking_id is not None:
        raise LegacyUploadError("attach the deposit to the parent booking, not a linked room")
    validate_pdf(pdf)

    existing = [i for i in booking.invoices if i.type == InvoiceType.deposit and i.status != InvoiceStatus.cancelled]
    live = [i for i in existing if not i.is_legacy]
    if live:
        raise LegacyUploadError("a live deposit invoice already exists for this booking")

    legacy = next((i for i in existing if i.is_legacy), None)
    paid_at = paid_at or dt.datetime.now(dt.timezone.utc)

    if legacy is None:
        legacy = Invoice(
            booking_id=booking.id, type=InvoiceType.deposit,
            line_items=[{"description": "Booking deposit (paid in iVvy, migrated)", "quantity": 1, "unit_price": str(amount)}],
            subtotal=amount, surcharge=Decimal("0.00"), total=amount,
            status=InvoiceStatus.paid, due_date=paid_at.date(), paid_at=paid_at,
            is_legacy=True, legacy_source_ref=source_ref, legacy_snapshot=booking_snapshot(booking),
        )
        db.add(legacy)
        db.flush()
        db.add(Payment(
            invoice_id=legacy.id, amount=amount, method=PaymentMethod.bank_transfer,
            reference=f"MIGRATED from iVvy ({source_ref}) -- legacy deposit, not a live payment date"
            if source_ref else "Legacy migrated deposit",
            received_at=paid_at,
        ))
    legacy.legacy_file = pdf
    legacy.legacy_filename = filename
    if source_ref:
        legacy.legacy_source_ref = source_ref
    legacy.legacy_snapshot = booking_snapshot(booking)

    db.add(BookingEvent(
        booking_id=booking.id, event_type="legacy_deposit_uploaded",
        field_name="deposit_invoice", new_value=(source_ref or filename), actor=actor,
    ))
    db.commit()
    db.refresh(legacy)
    return legacy
