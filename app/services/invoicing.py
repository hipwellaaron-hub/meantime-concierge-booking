"""Invoice generation and payment recording. Pricing math is delegated
entirely to app.services.policy -- this module applies those rules, it
doesn't define them.
"""

import datetime as dt
import logging
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Invoice, Payment, PublicHoliday
from app.services import booking as booking_service
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import stripe_integration
from app.services.policy import (
    PUBLIC_HOLIDAY_SURCHARGE_RATE,
    STANDARD_DEPOSIT,
    public_holiday_surcharge_applies,
)
from app.utils import is_valid_email

logger = logging.getLogger(__name__)

# The auto-added credit line on a final invoice (deposit already paid).
# Named as a constant so update_invoice can strip and re-derive it rather
# than treating it as a staff-entered charge line.
DEPOSIT_CREDIT_DESCRIPTION = "Less: deposit credited"


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def gst_component(gst_inclusive_amount: Decimal) -> Decimal:
    """Standard AU GST: 1/11 of a GST-inclusive amount. invoice.total is
    already GST-inclusive (nothing in this codebase adds GST on top of a
    quoted figure), so this is purely a breakdown for display, not an
    additional charge."""
    return _round_money(gst_inclusive_amount / Decimal("11"))


def line_item_breakdown(line_items: list[dict]) -> list[dict]:
    """Per-line GST split for the invoice view -- unit_price is always
    GST-inclusive (see gst_component above), so each line's own tax
    amount is derived the same way the invoice total's is, rather than
    just dividing the total's tax evenly across lines (which would drift
    from the true per-line figure whenever quantities/prices differ)."""
    rows = []
    for item in line_items:
        quantity = Decimal(str(item["quantity"]))
        unit_price = Decimal(str(item["unit_price"]))
        amount_incl = _round_money(quantity * unit_price)
        tax_amount = gst_component(amount_incl)
        rows.append({
            "description": item["description"],
            "quantity": item["quantity"],
            "unit_price": unit_price,
            "amount_incl": amount_incl,
            "tax_amount": tax_amount,
            "amount_excl": amount_incl - tax_amount,
        })
    return rows


def is_public_holiday(db: Session, event_date: dt.date) -> bool:
    holiday = db.execute(
        select(PublicHoliday).where(
            PublicHoliday.holiday_date == event_date,
            PublicHoliday.applies_to_surcharge.is_(True),
        )
    ).scalar_one_or_none()
    return holiday is not None


def compute_totals(db: Session, event_date: dt.date, line_items: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    try:
        subtotal = _round_money(
            sum(
                (Decimal(str(li["quantity"])) * Decimal(str(li["unit_price"])) for li in line_items),
                Decimal("0.00"),
            )
        )
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"malformed line item: {exc}") from exc

    # is_public_holiday answers False for a dateless booking, so it also
    # guards the date comparison that follows it. Order matters here.
    surcharge = Decimal("0.00")
    if is_public_holiday(db, event_date) and public_holiday_surcharge_applies(event_date):
        surcharge = _round_money(subtotal * PUBLIC_HOLIDAY_SURCHARGE_RATE)
    return subtotal, surcharge, subtotal + surcharge


def create_invoice(
    db: Session,
    booking: Booking,
    invoice_type: InvoiceType,
    line_items: list[dict],
    due_date: dt.date,
    *,
    actor: str,
    credit_line_items: list[dict] | None = None,
) -> Invoice:
    """credit_line_items (e.g. a negative "Less: deposit credited" line)
    are applied to `total` only, AFTER subtotal/surcharge are computed
    from `line_items` alone. They must never be passed through
    compute_totals mixed in with line_items -- the public holiday
    surcharge has to apply to the real gross charge, not a figure already
    reduced by a credit, or it silently undercharges. This is the same
    class of mistake that already produced a wrong invoice for a live
    client -- see app.services.wizard_generation.
    """
    if booking.parent_booking_id is not None:
        # Same reasoning as app.services.documents.create_new_version: a
        # linked child is a second room for the parent's event, not a
        # separate billable booking of its own.
        raise ValueError("cannot create an invoice on a linked booking -- use the parent booking instead")
    subtotal, surcharge, gross_total = compute_totals(db, booking.event_date, line_items)
    credit_line_items = credit_line_items or []
    credit_total = sum(
        (Decimal(str(c["quantity"])) * Decimal(str(c["unit_price"])) for c in credit_line_items), Decimal("0.00")
    )
    total = gross_total + credit_total  # credit unit_prices are negative, so this reduces total

    invoice = Invoice(
        booking_id=booking.id,
        type=invoice_type,
        line_items=line_items + credit_line_items,
        subtotal=subtotal,
        surcharge=surcharge,
        total=total,
        status=InvoiceStatus.draft,
        due_date=due_date,
    )
    db.add(invoice)
    db.flush()

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="invoice_created",
            field_name=f"{invoice_type.value}_invoice",
            new_value=str(total),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def deposit_figure_for(db: Session, booking: Booking) -> Decimal:
    """The deposit this booking's client was told about, or the house figure.

    The agreement freezes deposit_required at generation
    (document_generation.generate_agreement_content) because a signed
    contract reflects what was agreed. The deposit invoice then read
    policy.STANDARD_DEPOSIT live -- so the day that constant moves, a client
    holding a contract that says one figure gets an invoice for another.
    The same two-halves fault the minimum-spend clause had on 2026-09-05,
    one line down.

    The current agreement's figure wins when there is one. No agreement,
    or one that predates the key, and the house figure stands -- there is
    no contract to disagree with.
    """
    from app.models.document import DocumentType
    from app.services import documents as documents_service

    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)
    frozen = (agreement.content or {}).get("deposit_required") if agreement is not None else None
    if frozen not in (None, ""):
        try:
            return Decimal(str(frozen)).quantize(Decimal("0.01"))
        except InvalidOperation:
            pass
    return STANDARD_DEPOSIT


def create_deposit_invoice(db: Session, booking: Booking, *, due_date: dt.date, actor: str) -> Invoice:
    line_items = [{"description": "Booking deposit", "quantity": 1, "unit_price": str(deposit_figure_for(db, booking))}]
    return create_invoice(db, booking, InvoiceType.deposit, line_items, due_date, actor=actor)


def get_deposit_paid(db: Session, booking: Booking) -> Decimal:
    """Everything the client has actually paid towards a deposit on this
    booking, across every deposit invoice INCLUDING CANCELLED ONES.

    That inclusion is deliberate and it is the opposite of
    has_active_final_invoice below, which filters cancelled out. The
    asymmetry is real and it is written down here because the obvious
    "tidy-up" -- making the two consistent -- would silently start
    under-crediting clients for money they have handed over.

    Why counting a cancelled invoice's payments is the correct answer:

      * cancel_invoice REFUSES a paid invoice outright ("cannot cancel a
        paid invoice"), so a cancelled deposit invoice can only ever be one
        that was draft or sent -- never settled.
      * But a SENT deposit invoice can hold a PART payment: record_payment
        allows it and leaves the status at sent. Cancel that invoice (which
        booking._invalidate_client_facing_tokens does for every invoice
        when a booking goes terminal) and the payment row survives on a
        cancelled invoice.
      * The client still paid it. Concierge models no refunds -- there is
        no Refund row anywhere in this schema, and the word appears only in
        error messages telling a human to handle one outside the system --
        so nothing here can know the money went back. Dropping it from the
        count would credit a client less than they paid and bill them for
        the difference.

    The question this asks is "what has this client handed over", not
    "which invoices are alive". Those are different questions and only the
    first one belongs on a final invoice's deposit credit.
    """
    deposit_invoices = db.scalars(
        select(Invoice).where(Invoice.booking_id == booking.id, Invoice.type == InvoiceType.deposit)
    ).all()
    return sum((get_total_paid(db, inv.id) for inv in deposit_invoices), Decimal("0.00"))


def has_active_final_invoice(db: Session, booking: Booking) -> bool:
    return (
        db.execute(
            select(Invoice.id).where(
                Invoice.booking_id == booking.id,
                Invoice.type == InvoiceType.final,
                Invoice.status != InvoiceStatus.cancelled,
            )
        ).first()
        is not None
    )


def create_final_invoice(
    db: Session, booking: Booking, *, line_items: list[dict], due_date: dt.date, actor: str
) -> Invoice:
    """The manual, staff-facing counterpart to
    app.services.wizard_generation.generate_beo_and_invoice's automatic
    final invoice -- a booking whose client never completes the wizard
    (or never gets sent one) must still be invoiceable for the balance.
    Applies the same "Less: deposit credited" line automatically, so a
    manually-created final invoice can't accidentally double-charge a
    deposit that's already been paid.

    Refuses a second final invoice while a non-cancelled one already
    exists -- cancel or delete-if-draft the existing one first, same
    "surface, don't silently duplicate" rule as everywhere else in this
    module."""
    if has_active_final_invoice(db, booking):
        raise ValueError(
            "a final invoice already exists for this booking -- cancel or delete the existing one first"
        )

    credit_line_items = _deposit_credit_lines(db, booking)
    return create_invoice(
        db, booking, InvoiceType.final, line_items, due_date, actor=actor, credit_line_items=credit_line_items
    )


def _deposit_credit_lines(db: Session, booking: Booking) -> list[dict]:
    """The single auto "Less: deposit credited" line for a final invoice,
    or [] when no deposit has been paid. Shared by create_final_invoice and
    update_invoice so an edited invoice re-derives the credit from what's
    actually been paid rather than trusting a stale figure echoed back by
    the edit form."""
    deposit_paid = get_deposit_paid(db, booking)
    if deposit_paid <= 0:
        return []
    return [{"description": DEPOSIT_CREDIT_DESCRIPTION, "quantity": 1, "unit_price": str(-deposit_paid)}]


def _charge_lines(line_items: list[dict]) -> list[dict]:
    """The staff-entered charge lines, i.e. everything except the auto
    deposit-credit line. Discounts (negative unit_price) are charge lines
    and stay -- only the deposit credit is stripped, because it's
    system-derived and must be recomputed, never edited by hand."""
    return [li for li in line_items if li.get("description") != DEPOSIT_CREDIT_DESCRIPTION]


def delete_draft(db: Session, invoice: Invoice, *, actor: str) -> None:
    """Only a draft can be deleted -- same reasoning as
    app.services.documents.delete_draft. Anything sent/paid/cancelled must
    stay exactly as it is."""
    if invoice.status != InvoiceStatus.draft:
        raise ValueError(
            f"cannot delete an invoice that is already {invoice.status.value} -- only a draft can be deleted"
        )
    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="invoice_deleted",
            field_name=f"{invoice.type.value}_invoice",
            old_value=str(invoice.total),
            actor=actor,
        )
    )
    db.delete(invoice)
    db.commit()


def update_invoice(
    db: Session, invoice: Invoice, *, line_items: list[dict], due_date: dt.date, actor: str
) -> Invoice:
    """Edit a DRAFT invoice's line items and due date -- the staff lever
    for a discount (a line with a negative unit_price) or any amount
    change. Only a draft is editable: a sent invoice is a claim a client
    holds a real link at a stated figure, so it's revised via
    revise_sent_invoice (cancel + fresh draft) instead of silently
    changing underneath them.

    Totals are recomputed exactly as at creation -- the public-holiday
    surcharge on the true charge lines, then the deposit credit re-derived
    from what's actually been paid (never trusted from the submitted
    lines). A discount is a charge line and so correctly reduces the
    surcharge base; the deposit credit is not.
    """
    db.refresh(invoice, with_for_update=True)
    if invoice.is_legacy:
        raise ValueError("cannot edit a legacy invoice -- it is a fixed record of what was invoiced in iVvy")
    if invoice.status != InvoiceStatus.draft:
        raise ValueError(
            f"cannot edit an invoice that is already {invoice.status.value} -- "
            "revise a sent invoice (cancel + reissue) instead"
        )

    charge_lines = _charge_lines(line_items)
    if not charge_lines:
        raise ValueError("an invoice needs at least one line item")

    subtotal, surcharge, gross_total = compute_totals(db, invoice.booking.event_date, charge_lines)
    if subtotal < 0:
        # A discount larger than the charges it applies to is almost
        # certainly a data-entry slip, not a real negative invoice.
        raise ValueError("the discount is larger than the charges -- the invoice total can't be negative")

    credit_line_items = _deposit_credit_lines(db, invoice.booking) if invoice.type == InvoiceType.final else []
    credit_total = sum(
        (Decimal(str(c["quantity"])) * Decimal(str(c["unit_price"])) for c in credit_line_items), Decimal("0.00")
    )

    old_total = invoice.total
    invoice.line_items = charge_lines + credit_line_items
    invoice.subtotal = subtotal
    invoice.surcharge = surcharge
    invoice.total = gross_total + credit_total
    invoice.due_date = due_date

    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="invoice_edited",
            field_name=f"{invoice.type.value}_invoice",
            old_value=str(old_total),
            new_value=str(invoice.total),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def revise_sent_invoice(db: Session, invoice: Invoice, *, actor: str) -> Invoice:
    """Cancel a SENT invoice and return a fresh draft cloned from its
    charge lines, for staff to adjust (e.g. add a discount) and re-send.
    Refused once any payment exists -- a part-paid invoice is a
    reconciliation/refund question, not a quiet reissue. The deposit
    credit is dropped and re-derived by the new draft, so it always
    reflects what's genuinely been paid at reissue time.
    """
    db.refresh(invoice, with_for_update=True)
    if invoice.is_legacy:
        raise ValueError("cannot revise a legacy invoice -- it is a fixed record of what was invoiced in iVvy")
    if invoice.status != InvoiceStatus.sent:
        raise ValueError(f"only a sent invoice can be revised -- this one is {invoice.status.value}")
    if get_total_paid(db, invoice.id) > 0:
        raise ValueError(
            "this invoice already has a payment recorded -- handle the balance or a refund directly "
            "rather than reissuing it"
        )

    charge_lines = _charge_lines(invoice.line_items)
    invoice_type = invoice.type
    due_date = invoice.due_date
    booking = invoice.booking

    cancel_invoice(db, invoice, actor=actor)  # sent -> cancelled, logged

    if invoice_type == InvoiceType.final:
        return create_final_invoice(db, booking, line_items=charge_lines, due_date=due_date, actor=actor)
    return create_invoice(db, booking, invoice_type, charge_lines, due_date, actor=actor)


def get_by_token(db: Session, token: str) -> Invoice | None:
    return db.execute(select(Invoice).where(Invoice.access_token == token)).scalar_one_or_none()


def final_invoice_missing_deposit_credit(db: Session, booking: Booking) -> Invoice | None:
    """A SENT, unpaid final invoice whose deposit credit is out of date.

    _refresh_deposit_credit re-derives the credit on every edit and once
    more at mark_sent -- and never again. The case it cannot reach is the
    ordinary one: a final invoice goes out, and THEN the deposit is paid.
    The client is holding an invoice for the full food total, and the
    deposit they have since paid sits against a different invoice, so
    paying what they were asked for means paying the deposit twice.

    Deliberately DETECTS rather than fixes. An issued tax invoice is a
    record of what was asked for, and silently changing its total behind a
    client is not a safe automatic repair -- it is what Revise exists for,
    with a person deciding. This is the same rule reconciliation states
    for itself: "reads everything, fixes nothing, raises flags".

    Returns the invoice if it needs revising, else None.
    """
    invoice = db.scalars(
        select(Invoice).where(
            Invoice.booking_id == booking.id,
            Invoice.type == InvoiceType.final,
            Invoice.status == InvoiceStatus.sent,
        ).order_by(Invoice.created_at.desc())
    ).first()
    if invoice is None or invoice.is_legacy:
        return None
    if get_total_paid(db, invoice.id) > 0:
        # Part-paid: revising is refused anyway, and the balance is a
        # reconciliation question rather than a credit question.
        return None
    wanted = _deposit_credit_lines(db, booking)
    held = [li for li in (invoice.line_items or []) if li.get("description") == DEPOSIT_CREDIT_DESCRIPTION]
    # Compared as LINES, not as a total: the two differ exactly when the
    # credit is stale, and a total-level check would call a $500 credit and
    # a $500 discount the same thing.
    if held == wanted:
        return None
    return invoice


def uncredited_deposit(db: Session, invoice: Invoice) -> Decimal:
    """Deposit money the client has handed over that THIS final invoice does
    not yet credit. Zero for anything that is not an unpaid, native final.

    The companion to final_invoice_missing_deposit_credit, which only
    DETECTS the stale credit -- and detection reaches Triage, the digest and
    a staff banner, none of which the client sees. The client sees the
    invoice page, and the invoice page was still telling them to pay the
    full food total after their deposit had landed against a different
    invoice: paying what they were asked for paid the deposit twice.

    THE STORED INVOICE IS NOT TOUCHED. invoice.total is the record of what
    was asked for, and the detector's docstring is right that rewriting it
    behind a client is Revise's job. What a client needs is the OTHER
    figure every accounting package prints beside the total: what is
    payable now. That is derived here and rendered by
    app.api.invoices._build_invoice_context, so it is true of every invoice
    that already exists without regenerating any of them -- the same
    reasoning as templating.beo_billing.

    SUBTRACTS THE CREDIT ALREADY HELD ON THE LINES. A deposit part-paid
    before the invoice went out is already credited on it; crediting it
    again here would under-bill by exactly that amount, which is the same
    fault in the other direction. Only the difference is new information.
    """
    if invoice.type != InvoiceType.final or invoice.is_legacy:
        return Decimal("0.00")
    if invoice.status == InvoiceStatus.paid:
        return Decimal("0.00")
    held = sum(
        (
            -(Decimal(str(li.get("quantity", 0))) * Decimal(str(li.get("unit_price", 0))))
            for li in (invoice.line_items or [])
            if li.get("description") == DEPOSIT_CREDIT_DESCRIPTION
        ),
        Decimal("0.00"),
    )
    paid = get_deposit_paid(db, invoice.booking)
    return max(paid - held, Decimal("0.00"))


def _refresh_deposit_credit(db: Session, invoice: Invoice, *, actor: str) -> None:
    """Re-derive a final invoice's deposit credit from the payments
    actually recorded, at the moment it goes out.

    The credit is system-derived, never hand-typed -- update_invoice
    already re-derives it on every edit. What it could not cover is a
    draft that sat: an Event Order's food order is now approved (and its
    invoice built) well before the deposit is paid, so the draft carried
    no credit and the client would have been billed the deposit twice
    (review, 2026-09-11). Closes the same hole for a staff-built and a
    wizard-built draft, which could always sit in the same way."""
    if invoice.type != InvoiceType.final or invoice.is_legacy:
        return
    charge_lines = _charge_lines(invoice.line_items)
    credit_line_items = _deposit_credit_lines(db, invoice.booking)
    if charge_lines + credit_line_items == list(invoice.line_items or []):
        return
    subtotal, surcharge, gross_total = compute_totals(db, invoice.booking.event_date, charge_lines)
    credit_total = sum(
        (Decimal(str(c["quantity"])) * Decimal(str(c["unit_price"])) for c in credit_line_items), Decimal("0.00")
    )
    old_total = invoice.total
    invoice.line_items = charge_lines + credit_line_items
    invoice.subtotal = subtotal
    invoice.surcharge = surcharge
    invoice.total = gross_total + credit_total
    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="invoice_credit_rederived",
            field_name=f"{invoice.type.value}_invoice",
            old_value=str(old_total),
            new_value=str(invoice.total),
            actor=actor,
        )
    )


def mark_sent(db: Session, invoice: Invoice, *, actor: str) -> Invoice:
    # See record_payment below for why this lock matters: without it, two
    # concurrent calls could both pass a stale in-Python status check.
    db.refresh(invoice, with_for_update=True)
    if invoice.status != InvoiceStatus.draft:
        raise ValueError(f"cannot send an invoice that is already {invoice.status.value}")
    # Same reasoning as app.services.documents.mark_sent: "sent" is a
    # claim that a client has a real link, and a missing/malformed
    # address makes that claim false.
    contact = invoice.booking.contact
    if contact is None or not is_valid_email(contact.email):
        raise ValueError(
            "cannot send: this booking has no contact with a valid email address on file"
        )
    # Last responsible moment: a draft that has been sitting may predate
    # the deposit payment, and the credit is derived, never typed.
    _refresh_deposit_credit(db, invoice, actor=actor)
    old_status = invoice.status
    invoice.status = InvoiceStatus.sent
    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="invoice_status_changed",
            field_name="status",
            old_value=old_status.value,
            new_value=invoice.status.value,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(invoice)

    if invoice.type == InvoiceType.deposit:
        # Sending the deposit invoice is half of what holds the date; the
        # agreement is the other half (see booking.auto_hold_on_send). After
        # the commit and never raising -- the send must stand even if the
        # hold can't proceed, which is surfaced as a review flag instead.
        try:
            booking_service.auto_hold_on_send(db, invoice.booking, actor=actor)
        except Exception:  # noqa: BLE001 -- see above; a failure here must not undo a real send
            logger.exception("Auto-hold after sending deposit invoice failed for invoice %s", invoice.id)
    return invoice


def record_view(db: Session, invoice: Invoice) -> Invoice:
    """Called on the client's first GET of the public link. Sets
    viewed_at once, if not already set -- deliberately does not touch
    `status` (see the field's own comment on the model: every "unpaid"
    query already reads status == sent, and a viewed invoice is still
    exactly that)."""
    if invoice.viewed_at is None:
        invoice.viewed_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(invoice)
    return invoice


def cancel_invoice(db: Session, invoice: Invoice, *, actor: str) -> Invoice:
    db.refresh(invoice, with_for_update=True)
    if invoice.status == InvoiceStatus.paid:
        raise ValueError("cannot cancel a paid invoice")
    if invoice.status == InvoiceStatus.cancelled:
        # Idempotency, not just a nicety: without this, re-cancelling logs
        # a "cancelled -> cancelled" BookingEvent, which misrepresents the
        # audit trail as a real transition that never actually happened.
        raise ValueError("invoice is already cancelled")
    old_status = invoice.status
    invoice.status = InvoiceStatus.cancelled
    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="invoice_status_changed",
            field_name="status",
            old_value=old_status.value,
            new_value=invoice.status.value,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(invoice)
    _close_payment_links(invoice, why="cancellation")
    return invoice


def _close_payment_links(invoice: Invoice, *, why: str) -> None:
    """Deactivate every Payment Link ever minted for this invoice.

    Called from BOTH ends of an invoice's life -- cancellation and full
    payment -- because a Payment Link has no expiry of its own and a fresh
    one is minted on every invoice-page view and every PDF download, so an
    invoice a client opened three times has three links that stay payable
    forever.

    Cancellation used to be the only drain, which left the commonest exit
    -- actually paying -- with every one of those links still chargeable:
    the client, or anyone they forwarded the invoice email to, could tap
    an old link and pay a second time. `_invalidate_client_facing_tokens`
    could never rescue it either, since it skips any invoice already in
    INVOICE_TERMINAL_STATUSES, and `paid` is one of them. Found in review,
    2026-09-04.

    Never raises. By the time this runs the caller has committed real
    money or a real cancellation, and neither may fail because Stripe is
    unreachable -- a link left live is bad, undoing a committed payment is
    worse.
    """
    try:
        stripe_integration.deactivate_payment_links(invoice)
    except Exception:  # noqa: BLE001 -- see above
        logger.exception("Could not deactivate Payment Links for invoice %s after %s", invoice.id, why)


def record_payment_link(db: Session, invoice: Invoice, link_id: str, *, account: str = "") -> None:
    """Every Payment Link ever created for this invoice, so that both
    drains (_close_payment_links, from cancellation and from full payment)
    have something to deactivate. A fresh link is generated on each
    invoice-page view (see stripe_integration's own docstring), so there
    can be more than one live at a time."""
    # Stored with the account that minted it: a link can only be deactivated
    # through that account, and "whichever key is current" stops being it the
    # moment a second venue exists. Older entries are bare strings.
    entry = {"id": link_id, "account": account} if account else link_id
    invoice.stripe_payment_link_ids = [*(invoice.stripe_payment_link_ids or []), entry]
    db.commit()


def get_total_paid(db: Session, invoice_id: uuid.UUID) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.invoice_id == invoice_id)
    ).scalar_one()
    # Two places, always. coalesce(sum(...), 0) hands back the integer 0
    # when nothing has been paid, and Decimal(0) renders as "$0" on the
    # invoice beside a column of "$1450.00"s -- seen on HAM-1018's preview
    # on 2026-09-14. Every other money figure on that page is 2dp; this one
    # arriving from the database is not a reason for it to be the odd one.
    return Decimal(total).quantize(Decimal("0.01"))


def get_payment_summary(db: Session, invoice: Invoice) -> dict:
    """Supports split invoices: several payers can each pay part of the
    same invoice, and this reports where things stand in total rather than
    assuming a single payer/single payment."""
    total_paid = get_total_paid(db, invoice.id)
    payments = db.execute(
        select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.received_at)
    ).scalars().all()
    return {
        "total_paid": total_paid,
        "balance_due": invoice.total - total_paid,
        "is_fully_paid": total_paid >= invoice.total,
        "payments": payments,
    }


def record_payment(
    db: Session,
    invoice: Invoice,
    *,
    amount: Decimal,
    method: PaymentMethod,
    reference: str | None = None,
    payer_name: str | None = None,
    received_at: dt.datetime | None = None,
    actor: str,
    money_already_taken: bool = False,
) -> Payment:
    """Record a payment against a sent invoice.

    `money_already_taken` is the difference between a human typing a figure
    and a card processor reporting one, and it decides what an OVERPAYMENT
    does. Refusing is right for the first and catastrophic for the second:
    Stripe has the money either way, so a webhook that refuses leaves cash
    taken from a client with no record of it in Concierge -- silently worse
    than the overpayment it declined to write down. So staff are refused and
    can correct the number; the webhook records it and raises a flag.
    """
    if amount <= 0:
        raise ValueError("payment amount must be positive")

    # Locks the invoice for the rest of this transaction. This matters
    # most for split payments: two payers' payments landing moments apart
    # are two concurrent calls to this function against the same invoice.
    # Without the lock, both could compute total_paid from a snapshot
    # that doesn't yet include the other's (uncommitted) payment, and
    # neither would cross the "now fully paid" threshold -- the invoice
    # would stay stuck as unpaid even though it genuinely isn't anymore.
    db.refresh(invoice, with_for_update=True)

    if invoice.is_legacy:
        raise ValueError(
            "cannot record a payment against a legacy invoice -- it is a fixed record of a deposit already paid in iVvy"
        )
    if invoice.status == InvoiceStatus.cancelled:
        raise ValueError("cannot record a payment against a cancelled invoice")
    if invoice.status == InvoiceStatus.draft:
        # Out-of-order event: a draft hasn't been sent yet, so a client
        # can't have a token for it (see the draft-gating in
        # app/api/invoices.py) -- a payment here could only mean a bug in
        # whatever's calling this, not a real client payment.
        raise ValueError("cannot record a payment against a draft invoice -- send it first")

    # THE OVERPAYMENT GUARD. Read under the same lock as everything else
    # below, so two payments landing together cannot both see a balance
    # that the other is about to consume.
    #
    # Why this exists: a Stripe Payment Link carries a FROZEN amount -- the
    # balance as at the moment it was minted -- and every link an invoice
    # has ever minted stays live and payable. So a client who pays part of
    # a bill by transfer and then reopens an older invoice email is charged
    # the ORIGINAL total, with nothing having failed. Nothing here refused
    # it and nothing reported it.
    already_paid = get_total_paid(db, invoice.id)
    would_total = already_paid + amount
    if would_total > invoice.total:
        over_by = would_total - invoice.total
        if not money_already_taken:
            raise ValueError(
                f"${amount} would take this invoice to ${would_total} against a total of "
                f"${invoice.total} -- ${over_by} more than is owed. "
                f"${already_paid} is already recorded; the outstanding balance is "
                f"${invoice.total - already_paid}. Record the balance, or handle the "
                "difference as a refund rather than overpaying the invoice."
            )
        # Money already taken: recording it is not optional, so the write
        # proceeds and the flag is raised after it lands.

    received_at = received_at or dt.datetime.now(dt.timezone.utc)
    payment = Payment(
        invoice_id=invoice.id,
        amount=amount,
        method=method,
        reference=reference,
        payer_name=payer_name,
        received_at=received_at,
    )
    db.add(payment)
    db.flush()

    db.add(
        BookingEvent(
            booking_id=invoice.booking_id,
            event_type="payment_received",
            field_name="amount",
            new_value=str(amount),
            actor=actor,
        )
    )

    total_paid = get_total_paid(db, invoice.id)
    just_paid = total_paid >= invoice.total and invoice.status != InvoiceStatus.paid
    if just_paid:
        old_status = invoice.status
        invoice.status = InvoiceStatus.paid
        invoice.paid_at = received_at
        # Freeze the account the money actually went to. From here this
        # invoice is a RECEIPT, and a receipt that re-renders whatever bank
        # details the venue holds today is a record of nothing. Written
        # once, on the transition, so a later venue change cannot reach it.
        from app.templating import venue_identity

        identity = venue_identity(invoice.booking.venue)
        # EVERYTHING THE RECEIPT PRINTS ABOUT THE PAYEE, not just the bank
        # block. The first version froze five keys and the template read
        # three; the trading name, ABN and address in the header stayed
        # live, so a receipt already recorded which ACCOUNT was paid while
        # still reprinting whichever COMPANY the venue row named today --
        # the same fault, one section up (adversarial review, 2026-09-14).
        # The template asks one question, "what did this receipt say", and
        # this is the whole answer.
        invoice.paid_to_account = {
            "account_name": identity.get("venue_bank_account_name"),
            "bsb": identity.get("venue_bank_bsb"),
            "account_number": identity.get("venue_bank_account_number"),
            "legal_name": identity.get("venue_legal_name"),
            "trading_name": identity.get("venue_trading_name"),
            "abn": identity.get("venue_abn"),
            "address": identity.get("venue_address"),
            "frozen_at": received_at.isoformat(),
        }
        db.add(
            BookingEvent(
                booking_id=invoice.booking_id,
                event_type="invoice_status_changed",
                field_name="status",
                old_value=old_status.value,
                new_value="paid",
                actor=actor,
            )
        )

    db.commit()
    db.refresh(payment)

    if would_total > invoice.total:
        # Only reachable with money_already_taken -- the staff path raised
        # above. The client has paid more than they owe, so somebody has to
        # decide on a refund. After the commit and never raising: the
        # payment is real and recorded, and a failure to flag must not undo
        # it. flag_for_review puts it on the booking's own banner AND the
        # Triage flagged-bookings list, which is the difference between
        # this and payment_venue_mismatch -- an event nothing reads.
        try:
            booking_service.flag_for_review(
                db, invoice.booking,
                note=(
                    f"OVERPAID: {invoice.invoice_reference} has received ${get_total_paid(db, invoice.id)} "
                    f"against a total of ${invoice.total}. The last payment was ${amount}"
                    + (f" (ref {reference})" if reference else "")
                    + ". A card payment link carries the balance as at the moment it was minted, so an "
                    "older link pays the older, larger figure. Decide on a refund."
                ),
                actor=actor,
            )
        except Exception:  # noqa: BLE001 -- see above; a real payment must stand
            logger.exception("Could not flag overpayment on invoice %s", invoice.id)

    # EVERY payment closes the links, not just the settling one. A Payment
    # Link carries the balance as at the moment it was minted, so after a
    # part payment every earlier link still charges the pre-payment figure
    # -- the overpayment guard above then records the money and raises a
    # flag, which is the guard working, not the link being right. The next
    # invoice-page view mints a fresh link at what is payable now
    # (app.api.invoices._build_invoice_context), so the client is not
    # stranded, just asked to reopen the invoice.
    #
    # This reverses a 2026-09-04 decision to leave links alone on a part
    # payment, whose premise was that draining would leave "no way to pay
    # the rest". A fresh link on every view means there always was.
    #
    # After the commit and never raising, for the same reason the
    # auto-confirm below is: the payment is real and recorded, and nothing
    # here may undo it.
    _close_payment_links(invoice, why="full payment" if just_paid else "part payment")

    if invoice.type == InvoiceType.deposit and amount > 0:
        # THE OTHER INVOICE'S LINKS. A final invoice already out froze its
        # balance before this deposit existed; any link it minted charges
        # the full food total, and the overpayment guard cannot see that
        # because the amount IS the final's total. Close them; the next
        # view re-mints at payable_now, which credits the deposit.
        #
        # Queried, not read off invoice.booking.invoices: that collection is
        # whatever was loaded when something first touched it, and a final
        # invoice created later in the same session is not in it.
        for other in db.scalars(
            select(Invoice).where(
                Invoice.booking_id == invoice.booking_id,
                Invoice.type == InvoiceType.final,
                Invoice.status == InvoiceStatus.sent,
            )
        ).all():
            if other.id != invoice.id and not other.is_legacy:
                _close_payment_links(other, why="deposit paid on another invoice")
        # A deposit payment has just landed. If a final invoice is already
        # OUT, its deposit credit was frozen when it was sent and cannot
        # know about this -- so the client is holding a bill that will
        # charge them the deposit a second time. Flagged, not silently
        # revised: an issued tax invoice is a record, and Revise is the
        # action with a person behind it.
        #
        # After the commit and never raising, for the same reason as the
        # blocks around it: the payment is real and recorded, and a failure
        # to flag must not undo it.
        try:
            stale = final_invoice_missing_deposit_credit(db, invoice.booking)
            if stale is not None:
                booking_service.flag_for_review(
                    db, invoice.booking,
                    note=(
                        f"Final invoice {stale.invoice_reference} went out before this deposit was "
                        f"paid, so it carries no credit for the ${get_deposit_paid(db, invoice.booking)} "
                        "now received -- as it stands the client would pay the deposit twice. "
                        "Revise it before chasing the balance."
                    ),
                    actor=actor,
                )
        except Exception:  # noqa: BLE001 -- see above; a real payment must stand
            logger.exception("Could not check the final invoice credit on booking %s", invoice.booking_id)

    if just_paid and invoice.type == InvoiceType.deposit:
        # Paying the deposit is half of what confirms a booking; signing
        # the agreement is the other half (see
        # app.services.booking.auto_confirm_if_ready). Only on the payment
        # that actually clears the balance -- a part-payment of a split
        # deposit hasn't paid it yet.
        #
        # After the commit and never raising: the payment is real and
        # recorded, and neither a client's card payment nor Stripe's
        # webhook may fail because of what happens next. Same reasoning
        # app/api/webhooks.py already applies to a cancelled invoice.
        try:
            booking_service.auto_confirm_if_ready(db, invoice.booking, actor=actor)
        except Exception:  # noqa: BLE001 -- see above; a failure here must not undo a real payment
            logger.exception("Auto-confirm after deposit payment failed for invoice %s", invoice.id)

        # Alert the venue that the deposit is paid. After auto-confirm so
        # the email can say whether this payment has tipped the booking
        # into confirmed. Never raises (see notify_deposit_paid).
        from app.models.booking import BookingStatus
        from app.services import notifications

        booking = invoice.booking
        notifications.notify_deposit_paid(
            booking,
            amount=get_total_paid(db, invoice.id),
            agreement_signed=booking_service.has_signed_agreement(db, booking),
            now_confirmed=booking.status == BookingStatus.confirmed,
        )

    return payment


# The invoices a venue is "done with" -- fully settled or voided. Excluded
# from the default list (which is about what still needs attention), the
# mirror of TERMINAL_STATUSES for bookings.
INVOICE_TERMINAL_STATUSES = (InvoiceStatus.paid, InvoiceStatus.cancelled)


def search_invoices(
    db: Session,
    venue_id: uuid.UUID,
    *,
    status: InvoiceStatus | None = None,
    include_terminal: bool = False,
) -> list[Invoice]:
    """This venue's invoice register, newest first. By default the ones
    still needing attention (draft, sent); paid and cancelled are excluded
    unless a specific status is chosen or include_terminal is set.

    Scoped on invoices.venue_id, the same column the dashboard's own count
    uses, so the list and the tile that links to it cannot disagree. It
    used to join Booking -> Space for the same answer; the triggers make
    the two identical, and reading the column the schema advertises is the
    version somebody arriving at this code can trust.

    ORDERED BY THE REGISTER'S OWN NUMBER, not created_at. created_at is
    now(), which Postgres fixes at TRANSACTION START, while the number is
    taken later inside the insert -- so two invoices raised at the same
    moment could list in the opposite order to their numbers, silently. A
    register ordered by its own position cannot.
    """
    query = select(Invoice).where(Invoice.venue_id == venue_id)
    if status is not None:
        query = query.where(Invoice.status == status)
    elif not include_terminal:
        query = query.where(Invoice.status.not_in(INVOICE_TERMINAL_STATUSES))
    return list(db.scalars(query.order_by(Invoice.invoice_number.desc())))
