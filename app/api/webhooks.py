"""Stripe webhook receiver -- the other half of app/services/stripe_integration.py.

Reconciles card payments automatically: when a client pays via a
generated Payment Link, Stripe calls this endpoint, and we record the
payment against the matching invoice using the metadata set at link
creation time (never inferred from the amount).

Idempotency matters here specifically because Stripe documents that the
same event can be delivered more than once. Without a dedup check, a
redelivered webhook would double-record the same payment. Payment.reference
is used as the dedup key (the Stripe PaymentIntent ID, which is stable
across redeliveries of the same event).
"""

import uuid
from decimal import Decimal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Invoice, Payment
from app.models.payment import PaymentMethod
from app.services import booking as booking_service
from app.services import invoicing
from app.services.stripe_integration import INVOICE_METADATA_KEY, STRIPE_WEBHOOK_SECRET

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    if not STRIPE_WEBHOOK_SECRET:
        # Not configured yet -- fail loudly rather than pretend to accept
        # events we can't verify the authenticity of.
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload or signature") from exc

    if event["type"] == "checkout.session.completed":
        # construct_event returns a real stripe.checkout.Session object,
        # not a plain dict -- it has no .get(), so .to_dict() first
        # (found by testing: this crashed with AttributeError on every
        # real event, not just malformed ones).
        _handle_checkout_completed(db, event["data"]["object"].to_dict())

    # Always 200 on anything we understood but didn't act on (event types
    # we don't handle, missing/malformed metadata) -- returning an error
    # for those would make Stripe retry forever for no reason. Only a bad
    # signature (above) is rejected.
    return {"received": True}


def _handle_checkout_completed(db: Session, session: dict) -> None:
    metadata = session.get("metadata") or {}
    invoice_id_str = metadata.get(INVOICE_METADATA_KEY)
    payment_intent_id = session.get("payment_intent")
    amount_total = session.get("amount_total")

    if not invoice_id_str or not payment_intent_id or amount_total is None:
        return  # not one of our payment links, or an incomplete event -- nothing to reconcile

    try:
        invoice_id = uuid.UUID(invoice_id_str)
    except ValueError:
        return

    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return

    already_recorded = db.execute(select(Payment.id).where(Payment.reference == payment_intent_id)).first()
    if already_recorded is not None:
        return  # redelivered event for a payment we've already recorded

    amount = Decimal(amount_total) / Decimal(100)
    try:
        invoicing.record_payment(
            db,
            invoice,
            amount=amount,
            method=PaymentMethod.card,
            reference=payment_intent_id,
            actor="stripe_webhook",
        )
    except ValueError as exc:
        # The invoice was cancelled between link creation and payment --
        # the booking most likely went to a terminal status in between
        # (see app.services.booking.change_status, which deactivates a
        # cancelled invoice's Payment Links, but cannot undo a Stripe
        # checkout that had already started). Real money moved and this
        # system did not record where -- that must never be a silent
        # `return` again (an incident, 2026-09-04, made the risk obvious):
        # flag it on the booking so a human sees it on Triage and the
        # booking page, and does not have to notice it on a Stripe payout
        # weeks later.
        booking_service.flag_for_review(
            db, invoice.booking,
            note=(
                f"Stripe payment of ${amount} (reference {payment_intent_id}) landed on invoice "
                f"{invoice.invoice_number} after it was closed ({exc}). Needs a manual refund."
            ),
            actor="stripe_webhook",
        )
        return
