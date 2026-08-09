"""Stripe Payment Links integration.

Generates a one-time Payment Link for a specific invoice balance (not a
fixed reusable price -- the amount varies per invoice, and can shrink
after partial payments). A fresh link is created on every invoice-page
view rather than cached, since a cached link would go stale the moment a
partial payment changes the balance; Payment Links cost nothing to create
(Stripe only charges per completed transaction), so this is cheap.

Reconciliation happens via the webhook in app/api/webhooks.py, which
listens for `checkout.session.completed` and records the payment against
the matching invoice automatically -- see there for how the link back to
our own Invoice row works (a metadata field, not guessing from the
amount).
"""

import os
from decimal import ROUND_HALF_UP, Decimal

import stripe

from app.models import Invoice

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

# Metadata key on the Stripe Payment Link / Checkout Session that carries
# our own invoice ID -- the webhook uses this to find which invoice a
# completed payment belongs to. Never inferred from the amount alone.
INVOICE_METADATA_KEY = "concierge_invoice_id"


class StripeNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def create_payment_link(invoice: Invoice, amount: Decimal) -> str:
    """Create a Stripe Payment Link for `amount` against this invoice.
    Returns the payment link URL. Raises StripeNotConfigured if no key is
    set -- failing loudly beats silently faking a working payment flow.
    """
    if not is_configured():
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not set -- create a Stripe AU account and "
            "set the key before enabling card payment links."
        )

    description = f"{invoice.type.value.capitalize()} invoice — {invoice.booking.event_name} ({invoice.booking.reference_code})"

    payment_link = stripe.PaymentLink.create(
        line_items=[
            {
                "price_data": {
                    "currency": "aud",
                    "product_data": {"name": description},
                    "unit_amount": _to_cents(amount),
                },
                "quantity": 1,
            }
        ],
        metadata={INVOICE_METADATA_KEY: str(invoice.id)},
        after_completion={
            "type": "hosted_confirmation",
            "hosted_confirmation": {
                "custom_message": "Thanks — your payment has been received. We'll update your invoice shortly."
            },
        },
        api_key=STRIPE_SECRET_KEY,
    )
    return payment_link.url
