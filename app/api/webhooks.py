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

import logging
import os
import uuid
from decimal import Decimal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BookingEvent, Invoice, Payment, Venue
from app.models.payment import PaymentMethod
from app.services import booking as booking_service
from app.services import invoicing
from app.services.stripe_integration import INVOICE_METADATA_KEY, STRIPE_WEBHOOK_SECRET

router = APIRouter(tags=["webhooks"])
logger = logging.getLogger(__name__)


def _signing_secret_for(db: Session, venue_slug: str | None) -> tuple[str | None, object | None]:
    """(secret, venue). A venue names the environment variable holding its
    own signing secret, the same way it names its secret key -- never the
    secret itself on the row.

    With no slug (the legacy path) the answer is the process-wide secret and
    no venue, which is exactly today's behaviour.

    NO FALLBACK on the per-venue path, matching secret_key_for. A venue that
    has not named its own variable must NOT borrow the process-wide secret:
    its own account signs with its own secret, so every real event would
    fail verification, return 400, be retried by Stripe for about three days
    and then be dropped -- a client charged, an invoice that still says
    unpaid, and nothing raised on either side. Refusing is the loud version
    of the same outcome (review, 2026-09-12).

    Both refusals are LOGGED and distinguished. The refusal is loud to
    Stripe, whose delivery history the operator is not watching; without
    this, a slug mistyped into the Stripe dashboard and a variable not yet
    set on Railway look identical from inside Concierge.
    """
    if venue_slug is None:
        return STRIPE_WEBHOOK_SECRET, None
    venue = db.query(Venue).filter_by(slug=venue_slug).one_or_none()
    if venue is None:
        logger.error(
            "Stripe webhook refused: no venue with slug %r -- check the endpoint URL in "
            "the Stripe dashboard against the venue's slug",
            venue_slug,
        )
        return None, None
    name = getattr(venue, "stripe_webhook_secret_env", None)
    if not name:
        logger.error(
            "Stripe webhook refused: venue %r has not named its signing-secret variable "
            "(venues.stripe_webhook_secret_env is empty), and borrowing another venue's "
            "secret would fail verification on every event",
            venue_slug,
        )
        return None, venue
    secret = os.environ.get(name)
    if not secret:
        logger.error(
            "Stripe webhook refused: venue %r names %s, which is not set in this "
            "environment",
            venue_slug, name,
        )
    return secret, venue


async def _handle_stripe_event(
    request: Request, db: Session, *, venue_slug: str | None
):
    secret, venue = _signing_secret_for(db, venue_slug)
    if not secret:
        # Not configured yet -- fail loudly rather than pretend to accept
        # events we can't verify the authenticity of.
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload or signature") from exc

    if event["type"] == "checkout.session.completed":
        # construct_event returns a real stripe.checkout.Session object,
        # not a plain dict -- it has no .get(), so .to_dict() first
        # (found by testing: this crashed with AttributeError on every
        # real event, not just malformed ones).
        _handle_checkout_completed(db, event["data"]["object"].to_dict(), venue=venue)

    # Always 200 on anything we understood but didn't act on (event types
    # we don't handle, missing/malformed metadata) -- returning an error
    # for those would make Stripe retry forever for no reason. Only a bad
    # signature (above) is rejected.
    return {"received": True}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """The ORIGINAL path, unchanged and still live.

    Stripe's dashboard points every existing account here and live payments
    run through it daily. An interrupted webhook means a client pays and the
    system never knows, so this is retired only when the per-venue path below
    has been proven for an account and its dashboard moved -- deliberately,
    one account at a time, not by deploying a rename (Aaron, 2026-09-12).
    """
    return await _handle_stripe_event(request, db, venue_slug=None)


@router.post("/webhooks/stripe/{venue_slug}")
async def stripe_webhook_for_venue(venue_slug: str, request: Request, db: Session = Depends(get_db)):
    """One endpoint per venue, because construct_event verifies against
    exactly ONE signing secret and two companies' accounts sign with two.

    It is also what makes the venue assertion possible: the path says which
    account this event came from, so the invoice it names can be checked
    against it. Without that, a payment taken into the wrong company's
    account still verifies (that account signs its own event) and still
    records as a successful payment for the invoice.
    """
    return await _handle_stripe_event(request, db, venue_slug=venue_slug)


def _handle_checkout_completed(db: Session, session: dict, *, venue=None) -> None:
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

    # THE ASSERTION. The path said which account this event came from; the
    # invoice says which venue it belongs to. If they differ, the money went
    # into the wrong company's account and recording it would report a
    # successful payment for an invoice that has not been paid.
    #
    # Flagged, never recorded and never silently dropped: a client HAS paid,
    # somebody has to unpick it, and a silent return is how that stays
    # invisible until a reconciliation nobody ran.
    if venue is not None and invoice.booking.venue_id != venue.id:
        logger.error(
            "Stripe event for invoice %s arrived on venue %s's endpoint but the invoice belongs to venue %s "
            "-- NOT recording the payment; the money is in the wrong account and needs unpicking by hand",
            invoice.id, venue.slug, invoice.booking.venue_id,
        )
        db.add(
            BookingEvent(
                booking_id=invoice.booking_id,
                event_type="payment_venue_mismatch",
                field_name=f"{invoice.type.value}_invoice",
                old_value=str(venue.slug)[:500],
                new_value=str(payment_intent_id)[:500],
                actor="stripe_webhook",
            )
        )
        db.commit()
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
            # Stripe has the money. An overpayment here is recorded and
            # flagged rather than refused, because refusing would leave
            # cash taken from a client with no record of it in Concierge --
            # the same failure this handler's own except-branch below was
            # written for after the 2026-09-04 incident.
            money_already_taken=True,
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
                f"{invoice.invoice_reference} after it was closed ({exc}). Needs a manual refund."
            ),
            actor="stripe_webhook",
        )
        return
