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
from app.services.policy import (
    CARD_SURCHARGE_RATE,
    DEFAULT_CARD_NETWORK,
    PUBLIC_HOLIDAY_SURCHARGE_RATE,
    STANDARD_DEPOSIT,
    is_card_surcharge_permitted,
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

    surcharge = _round_money(subtotal * PUBLIC_HOLIDAY_SURCHARGE_RATE) if is_public_holiday(db, event_date) else Decimal("0.00")
    return subtotal, surcharge, subtotal + surcharge


def calculate_card_payment_amount(
    balance: Decimal, payment_date: dt.date, card_network: str = DEFAULT_CARD_NETWORK
) -> Decimal:
    """How much to actually charge a card for this balance, factoring in
    whether a surcharge may legally be applied on this date/network. Not
    baked into any stored invoice total -- see app/models/invoice.py."""
    if not is_card_surcharge_permitted(payment_date, card_network):
        return _round_money(balance)
    return _round_money(balance * (1 + CARD_SURCHARGE_RATE))


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


def create_deposit_invoice(db: Session, booking: Booking, *, due_date: dt.date, actor: str) -> Invoice:
    line_items = [{"description": "Booking deposit", "quantity": 1, "unit_price": str(STANDARD_DEPOSIT)}]
    return create_invoice(db, booking, InvoiceType.deposit, line_items, due_date, actor=actor)


def get_deposit_paid(db: Session, booking: Booking) -> Decimal:
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
    return invoice


def get_total_paid(db: Session, invoice_id: uuid.UUID) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.invoice_id == invoice_id)
    ).scalar_one()
    return Decimal(total)


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
) -> Payment:
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
    """Invoices across the venue, newest first. By default the ones still
    needing attention (draft, sent); paid and cancelled are excluded unless
    a specific status is chosen or include_terminal is set. Scoped through
    Booking -> Space to the venue the same way the dashboard's own counts
    are, so the list and the count on the tile that links to it can never
    disagree."""
    from app.models import Space

    query = (
        select(Invoice)
        .join(Booking, Invoice.booking_id == Booking.id)
        .join(Space, Booking.space_id == Space.id)
        .where(Space.venue_id == venue_id)
    )
    if status is not None:
        query = query.where(Invoice.status == status)
    elif not include_terminal:
        query = query.where(Invoice.status.not_in(INVOICE_TERMINAL_STATUSES))
    return list(db.scalars(query.order_by(Invoice.created_at.desc())))
