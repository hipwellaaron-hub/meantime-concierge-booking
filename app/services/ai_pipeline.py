"""Pipeline view: where every live booking actually is (brief section 3.0).

`stage` is not `status`. Status is what somebody set; stage is what has
actually happened, computed from documents, invoices and the wizard. That
distinction is the whole point -- it is what stops a booking being
remembered as signed when it was only sent.

Everything here derives from the same facts the automation already acts on
(the signed/paid gates and the sent-predicates behind auto_hold_on_send).
It deliberately does NOT reimplement those rules: if stage and the
automation could disagree, one of them would be lying, and this endpoint
exists precisely to be trusted.

Two honest limits, both reported rather than papered over:

- `replied` needs a recorded staff reply. Staff answer enquiries in Gmail,
  which Concierge never sees, so the only marker is a deliberate
  "reply logged" event. Until that action exists (a Tier 1 write in a
  later step) no booking computes as `replied`, and `awaiting` will
  over-report "staff" for enquiries already answered by email.
- `beo_sent` means the Event Order was issued, not that a client approved
  it. Only agreements can be signed in Concierge, so BEO approval is not a
  state that exists; `finalised` therefore rests on the final invoice
  being paid.
"""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Booking, Document, Space
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.wizard_session import WizardSessionStatus
from app.services.booking import TERMINAL_STATUSES

# Stage values, in pipeline order. Exported so the API validates a ?stage=
# filter against the real set rather than a second copy of the list.
STAGES = (
    "enquiry",
    "replied",
    "offered",
    "signed_unpaid",
    "paid_unsigned",
    "confirmed",
    "wizard_sent",
    "wizard_submitted",
    "beo_sent",
    "finalised",
    "archived",
)

# The event that makes `replied` real. Written by the "log reply" action
# (Tier 1 write, later step); nothing emits it yet.
REPLY_LOGGED_EVENT = "reply_logged"

# Events that mean the booking genuinely moved. days_at_stage measures from
# the most recent of these -- an AI note or a flag must not reset the clock
# on "this enquiry has sat untouched for eleven days".
ADVANCING_EVENTS = frozenset(
    {
        "status_changed",
        "document_created",
        "document_status_changed",
        "invoice_status_changed",
        "payment_received",
        "wizard_created",
        "wizard_submitted",
        REPLY_LOGGED_EVENT,
    }
)

# Actor prefixes meaning the *client* acted. Everything else (staff:, ai:,
# system:) counts as our side.
CLIENT_ACTOR_PREFIXES = ("client", "stripe_webhook")

# Statuses live enough to contest a slot -- the set the calendar shows,
# which is wider than BLOCKING_STATUSES because an enquiry doesn't hold a
# room but absolutely does mean somebody else is asking for it.
CONTESTING_STATUSES = (
    BookingStatus.enquiry,
    BookingStatus.offered,
    BookingStatus.tentative,
    BookingStatus.confirmed,
    BookingStatus.completed,
)


@dataclass
class PipelineRecord:
    booking: Booking
    stage: str
    awaiting: str
    last_activity_at: dt.datetime | None
    last_activity_by: str | None
    stage_since: dt.datetime | None
    days_at_stage: int | None
    contested: bool
    contested_with: list[str] = field(default_factory=list)


def _current(documents, doc_type: DocumentType) -> Document | None:
    for doc in documents:
        if doc.type == doc_type and doc.is_current:
            return doc
    return None


def times_overlap(a: Booking, b: Booking) -> bool:
    """Mirrors the database exclusion constraint: a NULL time range never
    conflicts, and touching endpoints do not overlap (half-open ranges).
    This is why Renee at lunch and Alyssa in the evening, both in the
    Mezzanine on 28 November, are not contesting each other."""
    if None in (a.start_time, a.end_time, b.start_time, b.end_time):
        return False
    return a.start_time < b.end_time and b.start_time < a.end_time


def compute_stage(booking: Booking, *, today: dt.date | None = None) -> str:
    """The furthest-along stage the stored facts support.

    Ordered latest-first: a confirmed booking whose wizard has gone out is
    at `wizard_sent`, not `confirmed`. `confirmed` therefore means both
    gates are met and the wizard has not been issued yet.
    """
    today = today or dt.date.today()

    if booking.status in TERMINAL_STATUSES:
        return "archived"
    if booking.event_date is not None and booking.event_date < today:
        return "archived"

    agreement = _current(booking.documents, DocumentType.agreement)
    signed = agreement is not None and agreement.status == DocumentStatus.signed
    agreement_sent = agreement is not None and agreement.status in (
        DocumentStatus.sent,
        DocumentStatus.viewed,
        DocumentStatus.signed,
    )

    deposits = [
        i for i in booking.invoices
        if i.type == InvoiceType.deposit and i.status != InvoiceStatus.cancelled
    ]
    deposit_paid = any(i.status == InvoiceStatus.paid for i in deposits)
    deposit_sent = any(i.status in (InvoiceStatus.sent, InvoiceStatus.paid) for i in deposits)
    final_paid = any(
        i.type == InvoiceType.final and i.status == InvoiceStatus.paid for i in booking.invoices
    )

    beo = _current(booking.documents, DocumentType.beo)
    beo_issued = beo is not None and beo.status in (
        DocumentStatus.sent,
        DocumentStatus.viewed,
        DocumentStatus.signed,
    )

    wizard = booking.wizard_session

    if final_paid:
        return "finalised"
    if beo_issued:
        return "beo_sent"
    if wizard is not None and wizard.status == WizardSessionStatus.submitted:
        return "wizard_submitted"
    if wizard is not None and wizard.status in (
        WizardSessionStatus.pending,
        WizardSessionStatus.in_progress,
    ):
        return "wizard_sent"
    if signed and deposit_paid:
        return "confirmed"
    if signed:
        return "signed_unpaid"
    if deposit_paid:
        return "paid_unsigned"
    if agreement_sent and deposit_sent:
        return "offered"
    if any(e.event_type == REPLY_LOGGED_EVENT for e in booking.events):
        return "replied"
    return "enquiry"


def _actor_side(actor: str | None) -> str:
    if not actor:
        return "staff"
    lowered = actor.lower()
    if any(lowered.startswith(prefix) for prefix in CLIENT_ACTOR_PREFIXES):
        return "client"
    return "staff"


def compute_awaiting(booking: Booking) -> tuple[str, dt.datetime | None, str | None]:
    """Who the ball is with, from who acted last.

    Only as good as what Concierge records: a staff reply sent from Gmail
    is invisible here, so this reads `staff` until a reply is logged. A
    known over-report, not something to quietly smooth over.
    """
    if not booking.events:
        return "staff", None, None
    # booking.events is ordered by created_at. Deliberately the last item
    # rather than max(): events written in one transaction share a single
    # now() timestamp, and max() would break that tie arbitrarily. Taking
    # the last matches both the relationship order and the order the audit
    # trail shows a human.
    last = booking.events[-1]
    side = _actor_side(last.actor)
    awaiting = "staff" if side == "client" else "client"
    return awaiting, last.created_at, last.actor


def compute_stage_since(booking: Booking) -> dt.datetime | None:
    advancing = [e for e in booking.events if e.event_type in ADVANCING_EVENTS]
    if advancing:
        return advancing[-1].created_at  # ordered; see compute_awaiting on ties
    return booking.created_at


def days_since(moment: dt.datetime | None, *, now: dt.datetime) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return max(0, (now - moment.astimezone(dt.timezone.utc)).days)


def load_pipeline_bookings(db: Session, venue) -> list[Booking]:
    """Every booking for this venue a pipeline view should consider.

    Linked children are excluded: a second room on the same event has no
    documents or invoices of its own (the parent owns them), so it would
    compute as a bare enquiry and read as a phantom deal. The parent
    reports its rooms via linked_spaces instead.
    """
    return list(
        db.scalars(
            select(Booking)
            .join(Space, Booking.space_id == Space.id)
            .where(Space.venue_id == venue.id, Booking.parent_booking_id.is_(None))
            .options(
                selectinload(Booking.documents),
                selectinload(Booking.invoices),
                selectinload(Booking.events),
                selectinload(Booking.space),
                selectinload(Booking.contact),
                selectinload(Booking.wizard_session),
                selectinload(Booking.linked_bookings).selectinload(Booking.space),
            )
            .order_by(Booking.event_date)
        ).all()
    )


def build_records(
    db: Session, venue, *, today: dt.date | None = None, now: dt.datetime | None = None
) -> list[PipelineRecord]:
    today = today or dt.date.today()
    now = now or dt.datetime.now(dt.timezone.utc)

    bookings = load_pipeline_bookings(db, venue)

    # Contested is computed across the whole live set in one pass rather
    # than a query per booking: same space, same date, overlapping times.
    live = [
        b for b in bookings
        if b.status in CONTESTING_STATUSES and b.event_date is not None
    ]
    by_slot: dict[tuple, list[Booking]] = {}
    for b in live:
        by_slot.setdefault((b.space_id, b.event_date), []).append(b)

    records = []
    for booking in bookings:
        awaiting, last_at, last_by = compute_awaiting(booking)
        stage_since = compute_stage_since(booking)

        rivals = []
        if booking.status in CONTESTING_STATUSES and booking.event_date is not None:
            for other in by_slot.get((booking.space_id, booking.event_date), []):
                if other.id != booking.id and times_overlap(booking, other):
                    rivals.append(other.reference_code)

        records.append(
            PipelineRecord(
                booking=booking,
                stage=compute_stage(booking, today=today),
                awaiting=awaiting,
                last_activity_at=last_at,
                last_activity_by=last_by,
                stage_since=stage_since,
                days_at_stage=days_since(stage_since, now=now),
                contested=bool(rivals),
                contested_with=sorted(rivals),
            )
        )
    return records
