"""Importer for the merged iVvy -> Concierge migration CSV
(concierge-import.csv), the one-time cutover of the remaining confirmed
iVvy bookings ahead of the mid-September changeover.

Distinct from app.services.ivvy_import (which expects iVvy's raw export
headers and drops bookings into an Unassigned/NULL-time triage space).
This file carries real space, real times and financial figures, so it
assigns the real room and time directly -- and the Postgres exclusion
constraint then gives free double-booking protection: a row that clashes
with something already in Concierge is refused per-row, never silently
duplicated.

What each booking gets, entirely from data (no PDF required):
- A confirmed Booking in its real space/time. pricing_locked_at is set
  from Opportunity Created Date so pre-May-2026 bookings keep the old
  pizza pricing; a row with no such date falls back to today and is named
  in the report so it can be fixed by hand.
- A signed agreement Document (placeholder content marked migrated; the
  original PDF is attached separately by the legacy-upload feature),
  which satisfies has_signed_agreement.
- A paid deposit Invoice + Payment built from total_paid (NOT assumed
  $500 -- a two-room booking paid more), which satisfies the deposit gate
  and feeds a later final invoice's deposit credit natively.

Grouping: rows are grouped by booking_code. A code with two rows is one
event across two rooms -- imported as one Booking with a linked space (the
second row's own pax is preserved on the linked row), never two bookings.

Deliberate, per Aaron's instructions:
- deposit_paid == "UNKNOWN" rows (this-weekend events outside the finance
  export) are SKIPPED -- they run on iVvy before the changeover.
- deposit_paid == "DUE" (Laura Solway) imports agreement-signed with the
  deposit OUTSTANDING (invoice sent, no payment).
- Negotiated minimums are NOT in the file; agreed_min_adults imports at the
  space default for correction by hand. No inference here.
- Idempotent on (migration_source, booking_code): re-running skips.
"""

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Space, Venue
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.payment import Payment, PaymentMethod
from app.models.booking_event import BookingEvent
from app.services import documents as documents_service
from app.services.booking import add_linked_space, create_booking
from app.services.legacy_documents import booking_snapshot
from app.services.contact_matching import find_or_create_contact
from app.utils import truncate

MIGRATION_SOURCE = "ivvy"
LAURA_STANDARD_DEPOSIT = Decimal("500.00")  # Laura's row carries no figure; the deposit that's now due

# Bookings that must NEVER be imported, excluded by CODE (not just by
# deposit_paid == UNKNOWN) so a later reissued file with filled financial
# columns still can't pull them in. Remove a code here only to import it.
#   - The four this-weekend events: run on iVvy before the changeover, kept
#     there as history (9LNFYZDXY2, DMEH7CYGAA, PT28HXDS7R, BDKHE288A5).
#   - NQMBB39MRP "Dr Matthew Holland - Christmas Party": already hand-entered
#     in Concierge (HAM-20261107-QS8U8, The Loft, 7 Nov 2026, deposit inv
#     #1011) under a DIFFERENT contact email, so the hand-entered-duplicate
#     guard can't match it -- exclude by code so it's never even attempted.
EXCLUDED_CODES = {"9LNFYZDXY2", "DMEH7CYGAA", "PT28HXDS7R", "BDKHE288A5", "NQMBB39MRP"}

# Explicit, auditable source-typo fixes -- applied loudly (a per-booking
# flag), never silently. `.clm` is never a real TLD, but we still correct
# only the exact known address rather than guessing at any `.clm`.
KNOWN_EMAIL_CORRECTIONS = {"emilyleamarsh@gmail.clm": "emilyleamarsh@gmail.com"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_SIGNED_RE = re.compile(r"Agreement signed\s+(\d{1,2})/(\d{1,2})/(\d{4})")
_DUE_RE = re.compile(r"Deposit due\s+(\d{1,2})/(\d{1,2})/(\d{4})")


@dataclass
class BookingResult:
    booking_code: str
    reference_code: str
    event_name: str
    spaces: list[str]
    deposit: str  # "paid:$X" | "outstanding:$X" | "none"
    flags: list[str] = field(default_factory=list)


@dataclass
class MigrationResult:
    created: list[BookingResult] = field(default_factory=list)
    skipped_unknown: list[str] = field(default_factory=list)      # deposit_paid UNKNOWN -> not imported
    skipped_excluded: list[str] = field(default_factory=list)     # in EXCLUDED_CODES -> never imported
    skipped_existing: list[str] = field(default_factory=list)     # already imported by iVvy code (idempotent)
    skipped_possible_duplicate: list[str] = field(default_factory=list)  # a hand-entered booking already exists
    errors: list[tuple[str, str]] = field(default_factory=list)   # (booking_code, reason)


def _clean_phone(value: str) -> str | None:
    # The export mangled some numbers with comma "thousands separators"
    # (+61,404,356,286). Keep only a leading + and digits.
    value = (value or "").strip()
    if not value:
        return None
    cleaned = re.sub(r"(?!^\+)[^\d]", "", value)  # strip everything except digits and a leading +
    cleaned = ("+" if value.lstrip().startswith("+") else "") + re.sub(r"\D", "", value)
    return cleaned or None


def _money(value: str) -> Decimal | None:
    value = (value or "").strip()
    if not value or value.upper() in {"UNKNOWN", "DUE"}:
        return None
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _time(value: str) -> dt.time | None:
    value = (value or "").strip()
    if not value:
        return None
    return dt.time.fromisoformat(value if len(value) > 5 else value + ":00" if value.count(":") == 1 else value)


def _proxy_dt(d: dt.date | None) -> dt.datetime | None:
    # A date-only proxy (opportunity created) rendered as midday UTC, the
    # same convention the manual back-dated-payment path uses, so the
    # calendar day can't slip either side of the venue's local day.
    if d is None:
        return None
    return dt.datetime.combine(d, dt.time(12, 0), tzinfo=dt.timezone.utc)


def _space_by_name(db: Session, venue: Venue, name: str) -> Space | None:
    return db.execute(
        select(Space).where(Space.venue_id == venue.id, Space.name == name.strip())
    ).scalar_one_or_none()


class _MalformedEmail(Exception):
    """Raised to refuse a row whose email is broken beyond a known fix --
    importing a booking no one can be emailed is worse than skipping it."""


def _resolve_email(raw: str) -> tuple[str, str | None]:
    """(email, flag). Applies an explicit known-typo correction with a loud
    flag; refuses anything still malformed rather than carrying it through."""
    email = (raw or "").strip()
    if email in KNOWN_EMAIL_CORRECTIONS:
        fixed = KNOWN_EMAIL_CORRECTIONS[email]
        return fixed, f"email corrected from {email!r} to {fixed!r} (known source typo)"
    if not _EMAIL_RE.match(email):
        raise _MalformedEmail(f"email {email!r} is malformed and has no known correction -- not imported; fix the source")
    return email, None


def _hand_entered_duplicate(db: Session, *, email: str, event_date: dt.date | None) -> Booking | None:
    """A booking already in Concierge that was NOT imported (migration_source
    NULL -- i.e. hand-entered or a real enquiry) for the same contact email
    and event date. The iVvy code can't catch these (they carry none), so
    they're the one way a duplicate could slip past idempotency. Terminal
    bookings (cancelled/dead/archived) don't count as a live clash."""
    if event_date is None:
        return None
    from app.models import Contact

    return db.execute(
        select(Booking)
        .join(Contact, Booking.contact_id == Contact.id)
        .where(
            Booking.migration_source.is_(None),
            Booking.event_date == event_date,
            func.lower(Contact.email) == email.strip().lower(),
            Booking.status.notin_([BookingStatus.cancelled, BookingStatus.dead, BookingStatus.archived]),
        )
    ).scalars().first()


def _mark_agreement_signed(db: Session, document, booking: Booking, *, signed_at: dt.datetime, signer_name: str, booking_code: str, actor: str) -> None:
    document.status = DocumentStatus.signed
    document.signed_at = signed_at
    document.signer_name = truncate(signer_name, 255)
    # Mark it a legacy record: read-only, never rendered to a client, and
    # snapshotted so a later change to the booking is flagged (see
    # app.services.legacy_documents). The signed PDF is uploaded against it
    # afterwards.
    document.is_legacy = True
    document.legacy_source_ref = booking_code
    document.legacy_snapshot = booking_snapshot(booking)
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type="agreement_signed",
            field_name="agreement",
            new_value=f"migrated from iVvy {booking_code}",
            actor=actor,
        )
    )
    db.commit()
    db.refresh(document)


def _import_group(db: Session, venue: Venue, rows: list[dict], code: str, actor: str, result: MigrationResult) -> None:
    primary_row = rows[0]

    # Never import an explicitly excluded booking, whatever its financial
    # columns say in this or any future reissued file (belt-and-braces over
    # the deposit_paid == UNKNOWN check below).
    if code in EXCLUDED_CODES:
        result.skipped_excluded.append(code)
        return

    # Idempotency: skip if this iVvy code is already imported.
    existing = db.execute(
        select(Booking.id).where(
            Booking.migration_source == MIGRATION_SOURCE, Booking.migration_external_ref == code
        )
    ).first()
    if existing is not None:
        result.skipped_existing.append(code)
        return

    deposit_flag = (primary_row.get("deposit_paid") or "").strip().upper()
    if deposit_flag == "UNKNOWN":
        result.skipped_unknown.append(code)
        return

    # --- contact + email ---------------------------------------------------
    name = (primary_row.get("contact_name") or "").strip()
    try:
        email, email_flag = _resolve_email(primary_row.get("contact_email"))
    except _MalformedEmail as exc:
        result.errors.append((code, str(exc)))
        return
    phone = _clean_phone(primary_row.get("contact_phone"))
    event_date = _date(primary_row["event_date"])

    # The one duplicate idempotency can't see: a hand-entered booking (no
    # iVvy code) for the same person and date. Skip it for manual
    # reconciliation rather than creating a second copy. The exclusion
    # constraint is only a backstop, and only for the time-overlapping case.
    dup = _hand_entered_duplicate(db, email=email, event_date=event_date)
    if dup is not None:
        result.skipped_possible_duplicate.append(f"{code} (matches existing {dup.reference_code})")
        return

    contact, _dupes = find_or_create_contact(db, truncate(name, 255), email, phone)

    opp_created = _date(primary_row.get("pricing_locked_at") or primary_row.get("opportunity_created"))
    flags: list[str] = []
    if email_flag:
        flags.append(email_flag)
    if opp_created is None:
        flags.append("pricing_locked_at defaulted to import date -- set the real Opportunity Created date by hand")

    company = (primary_row.get("company") or "").strip()
    comments = (primary_row.get("comments") or "").strip()
    note_lines = []
    if comments:
        note_lines.append(comments)
    if company:
        note_lines.append(f"Company: {company}")
    note_lines.append(f"Migrated from iVvy ({code}).")
    notes = "\n".join(note_lines)

    snapshot = {
        "ivvy_code": code,
        "proxy_dates_note": "agreement signed_at, deposit paid_at and payment received_at are the "
        "Opportunity Created date as a migration proxy -- NOT real signing/payment dates",
        "company": company or None,
        "coordinator": (primary_row.get("coordinator") or "").strip() or None,
        "beo_number": (primary_row.get("beo_number") or "").strip() or None,
        "layout": (primary_row.get("layout") or "").strip() or None,
        "pricing_basis": (primary_row.get("pricing_basis") or "").strip() or None,
        "food_total": str(_money(primary_row.get("food_total"))) if _money(primary_row.get("food_total")) is not None else None,
        "total_revenue": str(_money(primary_row.get("total_revenue"))) if _money(primary_row.get("total_revenue")) is not None else None,
        "total_paid": str(_money(primary_row.get("total_paid"))) if _money(primary_row.get("total_paid")) is not None else None,
        "total_outstanding": str(_money(primary_row.get("total_outstanding"))) if _money(primary_row.get("total_outstanding")) is not None else None,
    }

    # --- primary booking ---------------------------------------------------
    space = _space_by_name(db, venue, primary_row["space"])
    if space is None:
        result.errors.append((code, f"unknown space {primary_row['space']!r}"))
        return

    booking = create_booking(
        db,
        space_id=space.id,
        contact_id=contact.id,
        event_date=event_date,
        start_time=_time(primary_row.get("start_time")),
        end_time=_time(primary_row.get("end_time")),
        event_name=truncate(primary_row.get("event_name") or f"Imported booking {code}", 255),
        event_type=(primary_row.get("event_type") or "").strip() or None,
        adult_count=int(primary_row.get("pax") or 0),
        child_count=0,
        notes=notes,
        actor=actor,
        status=BookingStatus.confirmed,
        lead_source=(primary_row.get("lead_source") or "").strip() or None,
        migration_source=MIGRATION_SOURCE,
        migration_external_ref=code,
        migration_snapshot=snapshot,
        pricing_locked_at=opp_created,  # None -> create_booking defaults to today (flagged above)
    )

    spaces = [space.name]

    # --- linked space (a two-room event) -----------------------------------
    for extra in rows[1:]:
        extra_space = _space_by_name(db, venue, extra["space"])
        if extra_space is None:
            flags.append(f"unknown linked space {extra['space']!r} -- not linked")
            continue
        child = add_linked_space(
            db,
            booking,
            space_id=extra_space.id,
            start_time=_time(extra.get("start_time")),
            end_time=_time(extra.get("end_time")),
            actor=actor,
        )
        # add_linked_space mirrors the parent's pax; keep the linked room's
        # own figure from the file instead.
        child.adult_count = int(extra.get("pax") or 0)
        db.commit()
        spaces.append(extra_space.name)

    # --- signed agreement (satisfies has_signed_agreement) -----------------
    signed_at = _proxy_dt(opp_created) or _proxy_dt(event_date)
    m = _SIGNED_RE.search(comments)
    if m:
        signed_at = _proxy_dt(dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    agreement = documents_service.create_new_version(
        db,
        booking,
        DocumentType.agreement,
        {
            "migrated_from": "ivvy",
            "source_ref": code,
            "note": "Original agreement signed in iVvy prior to the Concierge migration. "
            "This is a record placeholder; attach the signed PDF via the legacy-document upload.",
        },
        actor=actor,
    )
    _mark_agreement_signed(
        db, agreement, booking, signed_at=signed_at, signer_name=f"{name} (migrated from iVvy)", booking_code=code, actor=actor
    )

    # --- deposit -----------------------------------------------------------
    # Built DIRECTLY, not via invoicing.record_payment, on purpose: a bulk
    # migration must not fire the per-payment "deposit paid" staff email for
    # every booking, and a historical figure must not pick up a live
    # public-holiday surcharge. `total` is the exact iVvy figure, so a later
    # final invoice's deposit credit (get_deposit_paid) reads it natively.
    deposit_desc = "none"
    if deposit_flag == "YES":
        amount = _money(primary_row.get("total_paid"))
        if amount is None or amount <= 0:
            flags.append("deposit marked paid but no total_paid figure -- deposit invoice/payment NOT created")
        else:
            due = opp_created or event_date
            # PROXY date, not accounting truth: iVvy's export has no payment
            # date, so both the invoice paid_at and the payment received_at
            # are the Opportunity Created date. The reference says so loudly
            # so no one later reads it as a real settlement date.
            paid_at = _proxy_dt(due)
            inv = Invoice(
                booking_id=booking.id, type=InvoiceType.deposit,
                line_items=[{"description": "Booking deposit (paid in iVvy, migrated)", "quantity": 1, "unit_price": str(amount)}],
                subtotal=amount, surcharge=Decimal("0.00"), total=amount,
                status=InvoiceStatus.paid, due_date=due, paid_at=paid_at,
                is_legacy=True, legacy_source_ref=code, legacy_snapshot=booking_snapshot(booking),
            )
            db.add(inv)
            db.flush()
            db.add(Payment(
                invoice_id=inv.id, amount=amount, method=PaymentMethod.bank_transfer,
                reference=f"MIGRATED from iVvy ({code}) -- date is Opportunity Created proxy, NOT actual payment date",
                received_at=paid_at,
            ))
            db.add(BookingEvent(booking_id=booking.id, event_type="invoice_created",
                                field_name="deposit_invoice", new_value=str(amount), actor=actor))
            db.add(BookingEvent(booking_id=booking.id, event_type="payment_received",
                                field_name="amount", new_value=str(amount), actor=actor))
            db.commit()
            deposit_desc = f"paid:${amount}"
    elif deposit_flag == "DUE":
        due = None
        dm = _DUE_RE.search(comments)
        if dm:
            due = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        due = due or event_date
        inv = Invoice(
            booking_id=booking.id, type=InvoiceType.deposit,
            line_items=[{"description": "Booking deposit", "quantity": 1, "unit_price": str(LAURA_STANDARD_DEPOSIT)}],
            subtotal=LAURA_STANDARD_DEPOSIT, surcharge=Decimal("0.00"), total=LAURA_STANDARD_DEPOSIT,
            status=InvoiceStatus.sent, due_date=due,  # sent + unpaid -> shows as outstanding
            is_legacy=True, legacy_source_ref=code, legacy_snapshot=booking_snapshot(booking),
        )
        db.add(inv)
        db.flush()
        db.add(BookingEvent(booking_id=booking.id, event_type="invoice_created",
                            field_name="deposit_invoice", new_value=str(LAURA_STANDARD_DEPOSIT), actor=actor))
        db.commit()
        deposit_desc = f"outstanding:${LAURA_STANDARD_DEPOSIT}"

    result.created.append(
        BookingResult(
            booking_code=code, reference_code=booking.reference_code,
            event_name=booking.event_name, spaces=spaces, deposit=deposit_desc, flags=flags,
        )
    )


@dataclass
class MigrationReport:
    """A READ-ONLY preview of what import_migration_csv would do -- writes
    nothing. Run this against production before the real import so the
    decisions (especially possible duplicates) can be eyeballed first."""
    would_create: list[dict] = field(default_factory=list)  # {code, event_name, spaces, deposit, flags}
    excluded: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    already_imported: list[str] = field(default_factory=list)
    possible_duplicate: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def report_migration_csv(db: Session, csv_path: str, *, venue: Venue) -> MigrationReport:
    """The same decisions import_migration_csv makes, but with no writes at
    all -- only SELECTs (idempotency + hand-entered-duplicate). Safe to run
    against production to produce a review report before importing."""
    report = MigrationReport()
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        all_rows = [r for r in csv.DictReader(f) if (r.get("booking_code") or "").strip()]
    groups: dict[str, list[dict]] = {}
    for row in all_rows:
        groups.setdefault(row["booking_code"].strip(), []).append(row)

    for code, rows in groups.items():
        primary = rows[0]
        if code in EXCLUDED_CODES:
            report.excluded.append(code)
            continue
        already = db.execute(
            select(Booking.id).where(
                Booking.migration_source == MIGRATION_SOURCE, Booking.migration_external_ref == code
            )
        ).first()
        if already is not None:
            report.already_imported.append(code)
            continue
        deposit_flag = (primary.get("deposit_paid") or "").strip().upper()
        if deposit_flag == "UNKNOWN":
            report.unknown.append(code)
            continue
        try:
            email, email_flag = _resolve_email(primary.get("contact_email"))
        except _MalformedEmail as exc:
            report.errors.append((code, str(exc)))
            continue
        event_date = _date(primary["event_date"])
        dup = _hand_entered_duplicate(db, email=email, event_date=event_date)
        if dup is not None:
            report.possible_duplicate.append(
                f"{code} — {primary.get('event_name')} on {event_date} "
                f"({email}) matches existing {dup.reference_code} ({dup.event_name})"
            )
            continue
        amount = _money(primary.get("total_paid"))
        deposit = (f"paid ${amount}" if deposit_flag == "YES" and amount else
                   f"outstanding ${LAURA_STANDARD_DEPOSIT}" if deposit_flag == "DUE" else "none")
        flags = [email_flag] if email_flag else []
        if _date(primary.get("pricing_locked_at") or primary.get("opportunity_created")) is None:
            flags.append("pricing_locked_at will default to import date (set by hand)")
        report.would_create.append({
            "code": code, "event_name": primary.get("event_name"), "event_date": str(event_date),
            "contact": f"{primary.get('contact_name')} <{email}>",
            "spaces": " + ".join(r["space"] for r in rows), "deposit": deposit, "flags": flags,
        })
    return report


def import_migration_csv(db: Session, csv_path: str, *, venue: Venue, actor: str = "ivvy_migration") -> MigrationResult:
    result = MigrationResult()

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        all_rows = [r for r in csv.DictReader(f) if (r.get("booking_code") or "").strip()]

    # Group by booking_code, preserving first-seen order (first row = primary).
    groups: dict[str, list[dict]] = {}
    for row in all_rows:
        groups.setdefault(row["booking_code"].strip(), []).append(row)

    for code, rows in groups.items():
        try:
            _import_group(db, venue, rows, code, actor, result)
        except Exception as exc:  # noqa: BLE001 -- one bad booking must not abort the batch
            db.rollback()
            result.errors.append((code, f"{type(exc).__name__}: {exc}"))

    return result
