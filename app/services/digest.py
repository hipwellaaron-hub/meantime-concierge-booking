"""Staff notification digest: what's building up that isn't urgent enough
for its own email, checked at a scheduled time rather than requiring
Aaron to open the dashboard to find out anything happened. A new enquiry
is deliberately NOT covered here -- it gets its own immediate email (see
app.services.enquiry_classification.notify_new_enquiry) the moment it
arrives, which is the whole point of that email; repeating it in the next
digest run would just be noise. This digest covers the two things that
are genuinely periodic rather than immediate: bookings that have become
wizard-ready, and invoices that have gone overdue.

Every section is self-clearing by construction (same worklist pattern as
app.services.enquiry_classification.get_enquiries_needing_clarification,
app.services.ivvy_import.get_unassigned_bookings, and
app.services.wizard.get_wizard_eligible_bookings) -- it reports current
state, not "what changed since the last digest". That needs no extra
state (no last-run timestamp to track, nothing to miss if a run fails or
is skipped) and matches how every other worklist in this app already
behaves: an item drops off once staff act on it, not once an email about
it was sent.
"""

import dataclasses
import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Invoice, Venue
from app.models.invoice import InvoiceStatus
from app.services import invoicing
from app.services.wizard import get_wizard_eligible_bookings


@dataclasses.dataclass
class OverdueInvoice:
    invoice: Invoice
    booking: Booking
    balance_due: Decimal


@dataclasses.dataclass
class DigestContent:
    wizard_eligible: list[Booking]
    overdue_invoices: list[OverdueInvoice]

    @property
    def is_empty(self) -> bool:
        return not (self.wizard_eligible or self.overdue_invoices)


def get_overdue_invoices(db: Session, venue: Venue, *, as_of: dt.date | None = None) -> list[OverdueInvoice]:
    """Sent invoices past their due date with a real balance still owing.
    Draft invoices are excluded -- a client was never shown one, so it
    can't be "overdue" on them. Cancelled and already-fully-paid invoices
    are excluded for the obvious reason."""
    as_of = as_of or dt.date.today()
    sent_invoices = list(
        db.scalars(
            select(Invoice)
            .join(Invoice.booking)
            .where(Invoice.status == InvoiceStatus.sent)
            .where(Invoice.due_date < as_of)
            .where(Invoice.booking.has(Booking.space.has(venue_id=venue.id)))
            .order_by(Invoice.due_date)
        ).all()
    )
    overdue = []
    for invoice in sent_invoices:
        balance_due = invoice.total - invoicing.get_total_paid(db, invoice.id)
        if balance_due > 0:
            overdue.append(OverdueInvoice(invoice=invoice, booking=invoice.booking, balance_due=balance_due))
    return overdue


def build_digest(db: Session, venue: Venue, *, as_of: dt.date | None = None) -> DigestContent:
    return DigestContent(
        wizard_eligible=get_wizard_eligible_bookings(db, venue, as_of=as_of),
        overdue_invoices=get_overdue_invoices(db, venue, as_of=as_of),
    )


def render_digest_text(content: DigestContent, *, dashboard_base_url: str) -> tuple[str, str]:
    """Returns (subject, plain-text body). Plain text, not HTML -- this is
    an internal operational email read in an inbox, not a client-facing
    document; it always renders correctly everywhere and needs no template."""
    total = len(content.wizard_eligible) + len(content.overdue_invoices)
    subject = f"Meantime Concierge: {total} item{'s' if total != 1 else ''} need attention" if total else "Meantime Concierge: all clear"

    lines = []

    if content.wizard_eligible:
        lines.append(f"READY FOR THE GUIDED WIZARD ({len(content.wizard_eligible)})")
        for b in content.wizard_eligible:
            lines.append(f"  - {b.event_name} ({b.event_date.isoformat()}) -- {dashboard_base_url}/admin/bookings/{b.id}")
        lines.append("")

    if content.overdue_invoices:
        lines.append(f"OVERDUE INVOICES ({len(content.overdue_invoices)})")
        for item in content.overdue_invoices:
            lines.append(
                f"  - {item.booking.event_name}: ${item.balance_due} overdue since {item.invoice.due_date.isoformat()} "
                f"-- {dashboard_base_url}/admin/bookings/{item.booking.id}"
            )
        lines.append("")

    if not lines:
        lines.append("Nothing needs attention right now.")

    body = "\n".join(lines)
    return subject, body
