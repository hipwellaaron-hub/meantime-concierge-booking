"""Nightly reconciliation (brief section 9).

Reads everything, fixes nothing, raises flags. That asymmetry is the point:
the highest-value thing read access buys is catching a problem before a
client does, and none of these problems has a safe automatic fix.

Findings are deduplicated on (booking, check) and clear themselves when the
condition goes away -- a run that no longer observes a finding resolves it,
so a problem fixed during the day is closed that night without anyone
dismissing anything.

Two of the brief's ten checks are not implemented, and deliberately so
rather than approximated:

- Section 9.6, "pricing_locked_at missing", cannot fire. The column is
  NOT NULL, so a booking without one does not exist. Building the query
  would add a check that can never return a row and imply cover that isn't
  real.
- Section 9.7, "linked-space booking where only one room has a record",
  is not detectable from what is stored. Nothing records that a second
  room was ever expected: the importer creates linked children from extra
  CSV rows, and migration_snapshot keeps ivvy_code, company, coordinator,
  beo_number, layout and totals -- no room list. In its place is
  SPLIT_EVENT, which catches the same failure from the other side: one
  contact with two same-day bookings in different rooms that are not
  linked to each other.
"""

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.services import policy

from app.models import (
    Booking,
    Contact,
    Document,
    Invoice,
    ReconciliationFinding,
    Space,
    Venue,
)
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.wizard_session import WizardSessionStatus
from app.services import booking as booking_service
from app.services.ai_pipeline import CONTESTING_STATUSES

logger = logging.getLogger(__name__)

# Event date this far out is implausible rather than merely distant.
FAR_FUTURE_MONTHS = 18
# "Event within the Event Order lead time and no BEO" -- the weekend check
# done by hand. One figure with the agreement clause and the wizard.
BEO_LEAD_DAYS = policy.EVENT_ORDER_LEAD_DAYS

# Categories from brief section 4.3, so a job finding and an AI flag are
# the same kind of thing on the Triage page.
DATA_MISMATCH = "data_mismatch"
MISSING_FIELD = "missing_field"
DATE_ANOMALY = "date_anomaly"
NEEDS_HUMAN = "needs_human"


@dataclass
class Finding:
    booking_id: uuid.UUID
    check_code: str
    category: str
    detail: str


@dataclass
class RunResult:
    opened: int = 0
    still_open: int = 0
    resolved: int = 0
    total_open: int = 0

    def as_dict(self) -> dict:
        return {
            "opened": self.opened,
            "still_open": self.still_open,
            "resolved": self.resolved,
            "total_open": self.total_open,
        }


def _active(db: Session, venue: Venue) -> list[Booking]:
    """Every non-terminal booking for the venue, with the relationships the
    checks need, loaded once."""
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Booking.status.notin_(booking_service.TERMINAL_STATUSES),
                # Parents only. A linked child is a second room on the same
                # event: it never owns an agreement, a deposit or an Event
                # Order (the parent does), so checking it would flag every
                # two-room booking twice and never clear -- Adrienne
                # Mckinney's Mezzanine appeared on Triage as a phantom
                # "confirmed without gates". The pipeline excludes children
                # for the same reason.
                Booking.parent_booking_id.is_(None),
            )
            .options(
                selectinload(Booking.documents),
                selectinload(Booking.invoices),
                selectinload(Booking.contact),
                selectinload(Booking.space),
                selectinload(Booking.wizard_session),
                selectinload(Booking.linked_bookings),
            )
            # Always reflect the live row, never a collection already
            # cached on an object in the session's identity map.
            .execution_options(populate_existing=True)
        ).all()
    )


# --- the checks ---------------------------------------------------------
# Each returns Findings. None of them writes anything.


def check_confirmed_without_email(bookings) -> list[Finding]:
    """Section 9.1. A confirmed booking with nowhere to send anything: the
    next send action fails, and it fails at the worst moment."""
    out = []
    for b in bookings:
        if b.status != BookingStatus.confirmed:
            continue
        if b.contact is None or not (b.contact.email or "").strip():
            out.append(
                Finding(b.id, "CONFIRMED_NO_EMAIL", MISSING_FIELD,
                        "Confirmed booking has no contact email -- any send will fail.")
            )
    return out


def check_date_anomalies(bookings, *, today: dt.date) -> list[Finding]:
    """Section 9.2. Nicole Jones (HAM-TBD-VXW04, dated 2028) is the case."""
    horizon = today + dt.timedelta(days=FAR_FUTURE_MONTHS * 30)
    out = []
    for b in bookings:
        # A TBD reference is only worth raising while it can still be fixed.
        # Once anything has been sent, the client holds that reference and
        # the booking service will deliberately keep it (see
        # booking.has_sent_anything) -- flagging it nightly would nag about
        # a decision, not a defect. A wrong DATE on such a booking is still
        # caught by DATE_TOO_FAR_OUT below, which is the actual error.
        if "TBD" in (b.reference_code or "") and not booking_service.has_sent_anything(b):
            out.append(
                Finding(b.id, "REFERENCE_TBD", DATE_ANOMALY,
                        f"Reference {b.reference_code} contains TBD -- the event date was never set. "
                        "Setting a date will regenerate it.")
            )
        if b.event_date is not None and b.event_date > horizon:
            out.append(
                Finding(b.id, "DATE_TOO_FAR_OUT", DATE_ANOMALY,
                        f"Event date {b.event_date.isoformat()} is more than {FAR_FUTURE_MONTHS} months away.")
            )
    return out


def check_confirmed_without_gates(bookings) -> list[Finding]:
    """Section 9.3, using status_pinned_at where the brief said
    deposit_waived -- that field does not exist, and a pin is exactly the
    record that a human deliberately set this status (Breast Cancer Trials,
    signed with the deposit waived). A pinned booking is a decision, not a
    discrepancy."""
    out = []
    for b in bookings:
        if b.status != BookingStatus.confirmed or b.status_pinned_at is not None:
            continue
        signed = any(
            d.type == DocumentType.agreement and d.is_current and d.status == DocumentStatus.signed
            for d in b.documents
        )
        paid = any(
            i.type == InvoiceType.deposit and i.status == InvoiceStatus.paid for i in b.invoices
        )
        if not (signed and paid):
            missing = []
            if not signed:
                missing.append("agreement not signed")
            if not paid:
                missing.append("deposit not paid")
            out.append(
                Finding(b.id, "CONFIRMED_WITHOUT_GATES", DATA_MISMATCH,
                        f"Confirmed but {' and '.join(missing)}, and the status was not set by hand.")
            )
    return out


def check_stale_holds(db: Session, venue: Venue, *, today: dt.date) -> list[Finding]:
    """Section 9.4. Reuses the same helper the dashboard tile uses, so the
    nightly job and the screen can never disagree about what needs chasing."""
    return [
        Finding(b.id, "STALE_HOLD", NEEDS_HUMAN,
                "Tentative hold on an issued but unpaid deposit, past its expiry -- chase or release.")
        for b in booking_service.get_holds_to_chase(db, venue.id, today=today)
    ]


def check_overlapping_open_interest(bookings) -> list[Finding]:
    """Section 9.5, scoped to enquiry and offered.

    The exclusion constraint already makes an overlap between blocking
    bookings impossible, so checking those would only ever confirm the
    database works. What it does NOT prevent is two open enquiries on the
    same room and time -- which is the actual double-offer risk.
    """
    live = [b for b in bookings
            if b.status in (BookingStatus.enquiry, BookingStatus.offered) and b.event_date]
    by_slot: dict[tuple, list[Booking]] = {}
    for b in live:
        by_slot.setdefault((b.space_id, b.event_date), []).append(b)

    out = []
    for group in by_slot.values():
        for b in group:
            rivals = [o.reference_code for o in group if o.id != b.id and booking_service.times_overlap(b, o)]
            if rivals:
                out.append(
                    Finding(b.id, "OVERLAPPING_OPEN_INTEREST", NEEDS_HUMAN,
                            f"Open interest overlaps {', '.join(sorted(rivals))} in the same room and time.")
                )
    return out


def check_split_events(bookings) -> list[Finding]:
    """Stands in for section 9.7 (see the module docstring).

    One contact, one date, two rooms, not linked to each other: either a
    two-room event recorded as two separate bookings, or a duplicate. Both
    need a human.
    """
    # Parents only. A linked child is already the correct representation of
    # a second room, so it must not be read as a split.
    by_key: dict[tuple, list[Booking]] = {}
    for b in bookings:
        if b.contact_id is None or b.event_date is None or b.parent_booking_id is not None:
            continue
        by_key.setdefault((b.contact_id, b.event_date), []).append(b)

    out = []
    for group in by_key.values():
        if len(group) < 2:
            continue
        for b in group:
            # A different room, same contact, same day, and neither is the
            # other's linked child (children were filtered out above, so any
            # remaining peer is a separate top-level booking).
            others = [o for o in group if o.id != b.id and o.space_id != b.space_id]
            if others:
                out.append(
                    Finding(b.id, "SPLIT_EVENT", DATA_MISMATCH,
                            "Same contact and date as "
                            f"{', '.join(sorted(o.reference_code for o in others))} "
                            "in another room, not linked -- one event across two rooms, or a duplicate.")
                )
    return out


def check_wizard_overdue(bookings, *, now: dt.datetime) -> list[Finding]:
    """Section 9.8, using the wizard link's expiry -- there is no separate
    wizard due date stored, and the expiry is the only real deadline."""
    out = []
    for b in bookings:
        w = b.wizard_session
        if w is None or w.status in (WizardSessionStatus.submitted, WizardSessionStatus.revoked):
            continue
        if w.expires_at is not None and w.expires_at < now:
            out.append(
                Finding(b.id, "WIZARD_OVERDUE", NEEDS_HUMAN,
                        "Wizard link has expired and the details were never submitted -- chase needed.")
            )
    return out


def check_imminent_without_beo(bookings, *, today: dt.date) -> list[Finding]:
    """Section 9.9 -- the weekend check, done by hand until now."""
    out = []
    for b in bookings:
        if b.event_date is None or not (today <= b.event_date <= today + dt.timedelta(days=BEO_LEAD_DAYS)):
            continue
        if b.status not in (BookingStatus.confirmed, BookingStatus.tentative):
            continue
        has_beo = any(
            d.type == DocumentType.beo and d.is_current
            and d.status in (DocumentStatus.sent, DocumentStatus.viewed, DocumentStatus.signed)
            for d in b.documents
        )
        if not has_beo:
            out.append(
                Finding(b.id, "IMMINENT_NO_BEO", NEEDS_HUMAN,
                        f"Event is on {b.event_date.isoformat()} and no Event Order has been issued.")
            )
    return out


def check_notes_before_beo(bookings) -> list[Finding]:
    """Bookings carrying free text whose Event Order has not been made yet.

    Until this build, booking.notes defaulted into the client-facing
    Special Notes of a generated Event Order, so anything sitting in it
    published itself the first time a BEO was made. That default is gone,
    but the text is still there and nobody has read it in months. This
    surfaces every booking holding some, before its Event Order is
    generated, because reading 47 of them by hand is not a thing anybody
    was going to do.

    Clears itself the moment the Event Order exists or the notes are
    emptied, like every other finding.
    """
    out = []
    for b in bookings:
        text = ((b.notes or "") + (b.enquiry_text or "")).strip()
        if not text:
            continue
        has_beo = any(d.type == DocumentType.beo for d in b.documents)
        if has_beo:
            continue
        out.append(
            Finding(
                b.id, "NOTES_BEFORE_BEO", NEEDS_HUMAN,
                "Carries enquiry text or internal notes and has no Event Order yet. Worth reading "
                "before one is generated: this text used to publish itself to the client.",
            )
        )
    return out


_DUPLICATED_NAME = re.compile(r"^\s*(.+?)\s+\1\s*$", re.IGNORECASE)
_PLAUSIBLE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_SUSPICIOUS_TLD = re.compile(r"\.(clm|con|cmo|couk|comm)$", re.IGNORECASE)


def check_contact_hygiene(bookings) -> list[Finding]:
    """Section 9.10. "Christine Hipwell Christine Hipwell" and the
    emilyleamarsh@gmail.clm typo are both real."""
    out = []
    for b in bookings:
        c = b.contact
        if c is None:
            continue
        if c.name and _DUPLICATED_NAME.match(c.name.strip()):
            out.append(
                Finding(b.id, "CONTACT_NAME_DUPLICATED", DATA_MISMATCH,
                        f"Contact name looks doubled: {c.name!r}.")
            )
        email = (c.email or "").strip()
        if email and (not _PLAUSIBLE_EMAIL.match(email) or _SUSPICIOUS_TLD.search(email)):
            out.append(
                Finding(b.id, "CONTACT_EMAIL_MALFORMED", DATA_MISMATCH,
                        f"Contact email looks malformed: {email!r}.")
            )
    return out


def collect(db: Session, venue: Venue, *, today: dt.date | None = None,
            now: dt.datetime | None = None) -> list[Finding]:
    """Every check, in the brief's priority order. Pure -- writes nothing."""
    today = today or dt.date.today()
    now = now or dt.datetime.now(dt.timezone.utc)
    bookings = _active(db, venue)

    findings: list[Finding] = []
    findings += check_confirmed_without_email(bookings)
    findings += check_date_anomalies(bookings, today=today)
    findings += check_confirmed_without_gates(bookings)
    findings += check_stale_holds(db, venue, today=today)
    findings += check_overlapping_open_interest(bookings)
    findings += check_split_events(bookings)
    findings += check_wizard_overdue(bookings, now=now)
    findings += check_imminent_without_beo(bookings, today=today)
    findings += check_contact_hygiene(bookings)
    findings += check_notes_before_beo(bookings)
    return findings


def run(db: Session, venue: Venue, *, today: dt.date | None = None,
        now: dt.datetime | None = None) -> RunResult:
    """Collect, then reconcile against what is already open.

    Opens new findings, touches surviving ones (without re-raising them),
    and resolves any open finding this run no longer sees.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    findings = collect(db, venue, today=today, now=now)
    seen = {(f.booking_id, f.check_code): f for f in findings}

    # THIS venue's rows only. Loading every finding here would make the
    # resolve loop below close another venue's open findings every time
    # this venue runs -- a bug the day there are two venues, so fixed
    # while there is one (Aaron's rule, 2026-09-03: jobs loop venues).
    existing = {
        (r.booking_id, r.check_code): r
        for r in db.scalars(
            select(ReconciliationFinding)
            .join(Booking, Booking.id == ReconciliationFinding.booking_id)
            .join(Space, Space.id == Booking.space_id)
            .where(Space.venue_id == venue.id)
        ).all()
    }

    result = RunResult()

    for key, finding in seen.items():
        row = existing.get(key)
        if row is None:
            db.add(
                ReconciliationFinding(
                    booking_id=finding.booking_id,
                    check_code=finding.check_code,
                    category=finding.category,
                    detail=finding.detail,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            result.opened += 1
        else:
            row.detail = finding.detail
            row.last_seen_at = now
            if row.resolved_at is not None:
                # Recurred: reopen the same row rather than starting a
                # second history for the same problem.
                row.resolved_at = None
                row.first_seen_at = now
                result.opened += 1
            else:
                result.still_open += 1

    for key, row in existing.items():
        if key not in seen and row.resolved_at is None:
            row.resolved_at = now
            result.resolved += 1

    db.commit()
    result.total_open = result.opened + result.still_open
    logger.info("Reconciliation for %s: %s", venue.slug, result.as_dict())
    return result


def open_findings(db: Session, venue: Venue) -> list[ReconciliationFinding]:
    return list(
        db.scalars(
            select(ReconciliationFinding)
            .join(Booking, ReconciliationFinding.booking_id == Booking.id)
            .join(Space, Booking.space_id == Space.id)
            .where(Space.venue_id == venue.id, ReconciliationFinding.resolved_at.is_(None))
            .options(selectinload(ReconciliationFinding.booking).selectinload(Booking.contact))
            .order_by(ReconciliationFinding.first_seen_at)
        ).all()
    )
