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

from app.utils import format_date_dmy
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Invoice, Venue
from app.models.invoice import InvoiceStatus
from app.services import invoicing
from app.services.wizard import get_wizard_eligible_bookings

# How many bookings are named under one reconciliation check before the
# rest are summarised. Enough that a small, urgent group is listed in
# full; small enough that a standing group of thirty cannot push the
# urgent one off the screen of a phone read first thing.
FINDINGS_LISTED_PER_CHECK = 5


@dataclasses.dataclass
class OverdueInvoice:
    invoice: Invoice
    booking: Booking
    balance_due: Decimal


@dataclasses.dataclass
class DigestContent:
    wizard_eligible: list[Booking]
    overdue_invoices: list[OverdueInvoice]
    # Aaron, 2026-09-14: "If a check fires and I don't hear about it, we've
    # built a log, not a safeguard." Until now the digest carried the two
    # sections above and nothing else, so every reconciliation check and
    # every flag -- including the overpayment guard shipped this morning --
    # reported to a page somebody had to remember to open.
    findings: list = dataclasses.field(default_factory=list)
    flagged_bookings: list[Booking] = dataclasses.field(default_factory=list)
    # The venue's own unfilled client-facing columns. NOT a reconciliation
    # finding, because a finding hangs off a booking (ReconciliationFinding
    # joins Booking -> Space -> venue to find its venue at all) and this
    # condition has no booking -- it is the reason there may never be one.
    # It lived in one line of the deploy log, which is read once, by
    # whoever ran the deploy, on the day it ran.
    venue_gaps: list[str] = dataclasses.field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.wizard_eligible
            or self.overdue_invoices
            or self.findings
            or self.flagged_bookings
            or self.venue_gaps
        )

    @property
    def item_count(self) -> int:
        """What the subject line counts. One implementation, so the number
        in the subject cannot disagree with the number of lines in the
        body -- the two used to be computed separately."""
        return (
            len(self.wizard_eligible)
            + len(self.overdue_invoices)
            + len(self.findings)
            + len(self.flagged_bookings)
            # ONE item, not one per column. Fourteen unfilled columns on a
            # venue that has not been set up yet is one job for one person,
            # and counting them individually would put "15 items need
            # attention" in the subject line over a single new venue and
            # bury the bookings under it.
            + (1 if self.venue_gaps else 0)
        )


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
            # invoices.venue_id rather than a correlated has() two levels
            # deep through Booking -> Space. Same rows, one predicate, and
            # it reads the column that is actually the invoice's venue.
            .where(Invoice.status == InvoiceStatus.sent)
            .where(Invoice.due_date < as_of)
            .where(Invoice.venue_id == venue.id)
            .order_by(Invoice.due_date)
        ).all()
    )
    overdue = []
    for invoice in sent_invoices:
        # What is PAYABLE NOW, not the invoice's own frozen balance. A final
        # invoice that went out before the deposit landed carries no credit
        # for it, and invoice.total - paid is the figure that bills the
        # deposit twice -- the same stale copy the client's page stopped
        # printing on 2026-09-14 (app.api.invoices._build_invoice_context).
        # A chase that names the wrong amount is worse than no chase.
        balance_due = invoice.total - invoicing.get_total_paid(db, invoice.id)
        balance_due -= invoicing.uncredited_deposit(db, invoice)
        if balance_due > 0:
            overdue.append(OverdueInvoice(invoice=invoice, booking=invoice.booking, balance_due=balance_due))
    return overdue


def build_digest(db: Session, venue: Venue, *, as_of: dt.date | None = None) -> DigestContent:
    """Imported here rather than at module scope: reconciliation imports
    booking_service, which imports this module's siblings, and a top-level
    import closes the cycle."""
    from app.services import enquiry_classification, reconciliation
    from app.seed import unfilled_columns

    return DigestContent(
        wizard_eligible=get_wizard_eligible_bookings(db, venue, as_of=as_of),
        overdue_invoices=get_overdue_invoices(db, venue, as_of=as_of),
        # The SAME helpers Triage renders, deliberately. The digest and the
        # page cannot then disagree about what is open -- the module's own
        # rule, and the reason every section here is self-clearing: this
        # reports current state, so a finding resolved during the day is
        # simply absent tomorrow with nothing to mark as sent.
        findings=reconciliation.open_findings(db, venue),
        flagged_bookings=enquiry_classification.get_flagged_bookings_in_progress(db, venue),
        # The SAME list the deploy log reports from, deliberately -- a
        # second copy of "which columns matter" is how the two come to
        # disagree about whether a venue is ready.
        venue_gaps=unfilled_columns(venue),
    )


def _item_lines(content: DigestContent, *, dashboard_base_url: str) -> list[str]:
    """One venue's worth of items. The single implementation of what an item
    looks like -- both the one-venue and the combined renderers go through
    it, so they cannot drift."""
    lines: list[str] = []

    if content.venue_gaps:
        # FIRST, above the bookings. A venue that cannot produce a correct
        # document outranks a reminder about one, and if reference_prefix
        # is among the gaps it cannot take a booking or issue an invoice
        # at all -- so every other section here would be empty for the
        # wrong reason.
        from app.seed import HARD_BLOCK_COLUMNS

        blocking = [c for c in content.venue_gaps if c in HARD_BLOCK_COLUMNS]
        lines.append(f"VENUE SET-UP INCOMPLETE ({len(content.venue_gaps)} unfilled)")
        lines.append(f"  - {', '.join(content.venue_gaps)}")
        lines.append("  - These print BLANK on invoices, agreements and Event Orders.")
        if blocking:
            lines.append(
                f"  - {', '.join(blocking)} is worse than blank: this venue cannot take a "
                "booking or issue an invoice until it is set."
            )
        # No link: nothing in the admin edits a venue row (checked
        # 2026-09-14 -- there is no /admin/venues route), so a link here
        # would be a 404 in an email sent at 20:30.
        lines.append("  - Set on the venues row directly; no admin page edits these yet.")
        lines.append("")

    if content.wizard_eligible:
        lines.append(f"READY FOR THE GUIDED WIZARD ({len(content.wizard_eligible)})")
        for b in content.wizard_eligible:
            lines.append(f"  - {b.event_name} ({format_date_dmy(b.event_date)}) -- {dashboard_base_url}/admin/bookings/{b.id}")
        lines.append("")

    if content.overdue_invoices:
        lines.append(f"OVERDUE INVOICES ({len(content.overdue_invoices)})")
        for item in content.overdue_invoices:
            lines.append(
                f"  - {item.booking.event_name}: ${item.balance_due} overdue since {format_date_dmy(item.invoice.due_date)} "
                f"-- {dashboard_base_url}/admin/bookings/{item.booking.id}"
            )
        lines.append("")

    if content.flagged_bookings:
        # Flags first among the new sections: a flag is raised by a person
        # or by something that just happened to money (the overpayment
        # guard raises one), where a finding is a standing condition.
        lines.append(f"FLAGGED, STILL OPEN ({len(content.flagged_bookings)})")
        for b in content.flagged_bookings:
            lines.append(
                f"  - {b.event_name} ({format_date_dmy(b.event_date) if b.event_date else 'date TBD'}) "
                f"-- {dashboard_base_url}/admin/bookings/{b.id}"
            )
        lines.append("")

    if content.findings:
        # GROUPED BY CHECK, and that is the whole design of this section.
        # Listing every finding flat is what made Triage unreadable: 37
        # migration-era CONFIRMED_WITHOUT_GATES rows buried four IMMINENT
        # bookings inside a fortnight, and Aaron nearly missed them. Grouped,
        # the 37 collapse to one line with a count and the urgent check gets
        # its own heading.
        by_check: dict[str, list] = {}
        for f in content.findings:
            by_check.setdefault(f.check_code, []).append(f)

        lines.append(f"RECONCILIATION ({len(content.findings)} open)")
        # Smallest groups first: a check with three bookings is nearly always
        # the one that needs reading, and a check with thirty is nearly
        # always a standing condition somebody already knows about.
        for check_code, group in sorted(by_check.items(), key=lambda kv: (len(kv[1]), kv[0])):
            label = check_code.replace("_", " ").title()
            lines.append(f"  {label} ({len(group)})")
            for f in group[:FINDINGS_LISTED_PER_CHECK]:
                booking = f.booking
                lines.append(
                    f"    - {booking.event_name} "
                    f"({format_date_dmy(booking.event_date) if booking.event_date else 'date TBD'}): "
                    f"{f.detail} -- {dashboard_base_url}/admin/bookings/{booking.id}"
                )
            remaining = len(group) - FINDINGS_LISTED_PER_CHECK
            if remaining > 0:
                # Said out loud rather than silently truncated: a section
                # that quietly shows five of thirty reads as "there are
                # five".
                lines.append(f"    ... and {remaining} more on Triage")
        lines.append("")

    return lines


def render_digest_text(content: DigestContent, *, dashboard_base_url: str) -> tuple[str, str]:
    """Returns (subject, plain-text body) for a SINGLE venue, with no venue
    heading -- the shape the digest had before there were two venues.

    Plain text, not HTML -- this is an internal operational email read in an
    inbox, not a client-facing document; it always renders correctly
    everywhere and needs no template."""
    total = content.item_count
    subject = f"Meantime Concierge: {total} item{'s' if total != 1 else ''} need attention" if total else "Meantime Concierge: all clear"

    lines = _item_lines(content, dashboard_base_url=dashboard_base_url)
    if not lines:
        lines.append("Nothing needs attention right now.")

    return subject, "\n".join(lines)


def render_combined_digest(
    per_venue: list[tuple[Venue, DigestContent]], *, dashboard_base_url: str
) -> tuple[str, str]:
    """One email covering several venues, a section each.

    Aaron, 2026-09-12: "one email with the venues sectioned, since I read it
    on a phone first thing and two emails means one gets skimmed." The length
    is one line per item under two headings per venue, and the item count is
    bounded by what is actually actionable -- functions inside the 14-day
    wizard window that still need theirs, plus overdue invoices -- so two
    venues stays short.

    A venue with nothing outstanding still gets a heading saying so, rather
    than being omitted. An absent section is ambiguous: it reads as "nothing
    to do" and "that venue was not checked" identically, and the second is
    the one that matters after a venue is added and something fails to wire
    up.
    """
    # content.item_count, not a second hand-written sum: the single-venue
    # renderer already computes this one way, and two copies of "how many
    # items is that" is exactly how a subject line comes to disagree with
    # the body it summarises.
    totals = {venue.id: content.item_count for venue, content in per_venue}
    total = sum(totals.values())
    subject = (
        f"Meantime Concierge: {total} item{'s' if total != 1 else ''} need attention"
        if total else "Meantime Concierge: all clear"
    )

    # With one venue the body is exactly what it has always been -- no
    # heading, no separator -- so nothing changes for a single-venue business.
    if len(per_venue) == 1:
        return render_digest_text(per_venue[0][1], dashboard_base_url=dashboard_base_url)

    lines: list[str] = []
    for venue, content in per_venue:
        heading = (venue.trading_name or venue.name).upper()
        count = totals[venue.id]
        lines.append(f"=== {heading} ({count}) ===")
        lines.append("")
        section = _item_lines(content, dashboard_base_url=dashboard_base_url)
        if section:
            lines.extend(section)
        else:
            lines.append("Nothing needs attention right now.")
            lines.append("")

    return subject, "\n".join(lines).rstrip() + "\n"
