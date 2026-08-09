"""Stripe Payment Links integration seam.

Not wired to a live account: there is no Stripe account or API key
available at build time, and creating one is account-creation/KYC that
only Aaron can do -- not something to fabricate credentials for or fake a
working flow around. This module defines the interface so that wiring in
a real key later is a config change, not a rewrite. See the Phase 3 build
notes for the cost/recommendation writeup.
"""

import os
from decimal import Decimal

from app.models import Invoice

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")


class StripeNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


def create_payment_link(invoice: Invoice, amount: Decimal) -> str:
    """Create a Stripe Payment Link for `amount` against this invoice.

    Raises StripeNotConfigured until STRIPE_SECRET_KEY is set. That's
    deliberate: failing loudly beats silently faking a working payment
    flow with no real Stripe account behind it.
    """
    if not is_configured():
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not set -- create a Stripe AU account and "
            "set the key before enabling card payment links."
        )
    raise NotImplementedError("Wire in the real Stripe API call once a live account/key exists.")
