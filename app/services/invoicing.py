"""Invoice generation and payment recording. Pricing math is delegated
entirely to app.services.policy -- this module applies those rules, it
doesn't define them.
"""

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Invoice, Payment, PublicHoliday
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services.policy import (
    CARD_SURCHARGE_RATE,
    DEFAULT_CARD_NETWORK,
    PUBLIC_HOLIDAY_SURCHARGE_RATE,
    STANDARD_DEPOSIT,
    is_card_surcharge_permitted,
)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


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


def get_by_token(db: Session, token: str) -> Invoice | None:
    return db.execute(select(Invoice).where(Invoice.access_token == token)).scalar_one_or_none()


def mark_sent(db: Session, invoice: Invoice, *, actor: str) -> Invoice:
    # See record_payment below for why this lock matters: without it, two
    # concurrent calls could both pass a stale in-Python status check.
    db.refresh(invoice, with_for_update=True)
    if invoice.status != InvoiceStatus.draft:
        raise ValueError(f"cannot send an invoice that is already {invoice.status.value}")
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
    if total_paid >= invoice.total and invoice.status != InvoiceStatus.paid:
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
    return payment
