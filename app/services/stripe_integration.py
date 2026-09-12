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

import enum
import logging
import os
from decimal import ROUND_HALF_UP, Decimal

import stripe

from app.models import Invoice

logger = logging.getLogger(__name__)

# Hamilton's, and the DEFAULT name a venue points at. Read at import, which
# is fine for a value that cannot change without a redeploy.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")


class StripeVenueMismatch(RuntimeError):
    """The resolved credential does not belong to the venue it was resolved
    for. Never caught and turned into a link: a payment link minted in the
    wrong company's account is signed by that account, passes verification,
    and records as a successful payment for the other company."""


def secret_key_for(venue) -> str:
    """This venue's Stripe secret, by the ENVIRONMENT VARIABLE NAME stored on
    the venue row -- never a key stored in the database.

    No fallback to STRIPE_SECRET_KEY when the venue names a variable that is
    not set: falling back would mint the link in Hamilton's account for
    whichever venue asked, which is the exact failure this exists to stop.
    A venue with no name set at all is the un-migrated case and does fall
    through, because that IS Hamilton today.
    """
    name = getattr(venue, "stripe_secret_key_env", None) or "STRIPE_SECRET_KEY"
    key = os.environ.get(name)
    if not key:
        raise StripeNotConfigured(
            f"{name} is not set, so no payment link can be created for "
            f"{getattr(venue, 'slug', 'this venue')!r}"
        )
    return key


def assert_key_belongs_to(venue, api_key: str) -> None:
    """Check the resolved key against the account the venue says it should
    be, and refuse if they differ.

    Only checks when the venue HAS an expected account id. A venue that has
    not been given one is not silently trusted -- it is simply not yet
    checkable, and that gap is why stripe_account_id exists on the row.

    One network call per link creation. Accepted: it is the only thing
    standing between a mis-keyed credential and a payment recorded as
    successful for the wrong company, and links are created per invoice
    view, not per request.
    """
    expected = getattr(venue, "stripe_account_id", None)
    if not expected:
        return
    try:
        account = stripe.Account.retrieve(api_key=api_key)
    except stripe.StripeError as exc:
        raise StripeVenueMismatch(
            f"could not confirm which Stripe account this key belongs to: {exc}"
        ) from exc
    if account.get("id") != expected:
        raise StripeVenueMismatch(
            f"the resolved Stripe key belongs to account {account.get('id')!r}, but "
            f"{getattr(venue, 'slug', 'this venue')!r} expects {expected!r} -- refusing to "
            "create a payment link that would take money into the wrong account"
        )

# Metadata key on the Stripe Payment Link / Checkout Session that carries
# our own invoice ID -- the webhook uses this to find which invoice a
# completed payment belongs to. Never inferred from the amount alone.
INVOICE_METADATA_KEY = "concierge_invoice_id"


class StripeNotConfigured(RuntimeError):
    pass


class StripeMode(str, enum.Enum):
    live = "live"
    test = "test"
    not_configured = "not_configured"


def is_configured() -> bool:
    """Process-level: is ANY Stripe key set. Still used by the admin's
    live/test badge, which has no venue in scope. Not the right question
    for a payment link -- see is_configured_for."""
    return bool(STRIPE_SECRET_KEY)


def is_configured_for(venue) -> bool:
    """Whether a payment link can be created for THIS venue. One venue can
    be configured while another is not, which a process-level check cannot
    express -- and answering the wrong question here is how an unconfigured
    venue would get Hamilton's key."""
    try:
        secret_key_for(venue)
    except StripeNotConfigured:
        return False
    return True


def get_mode() -> StripeMode:
    """Derived from the secret key's own prefix, never from a separate
    setting -- a separate "is this live?" flag can silently disagree with
    which key is actually loaded (wrong env var set, a stale value left
    over from a previous config), and that's exactly the mistake this
    exists to make impossible. Stripe's real key shapes: sk_test_... /
    rk_test_... for test/restricted-test keys, sk_live_... / rk_live_...
    for the real thing.

    Only an explicitly-recognized test-key shape is ever reported as
    "test" -- anything else non-empty (a real live key, or a shape this
    hasn't seen before) is reported as "live". Money is the one place
    where an unrecognized case must fail toward "assume this is real",
    never toward "assume it's safe to ignore"."""
    if not STRIPE_SECRET_KEY:
        return StripeMode.not_configured
    if STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
        return StripeMode.test
    return StripeMode.live


def _to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def create_payment_link(invoice: Invoice, amount: Decimal) -> tuple[str, str, str]:
    """Create a Stripe Payment Link for `amount` against this invoice.
    Returns (url, payment_link_id). Raises StripeNotConfigured if no key
    is set -- failing loudly beats silently faking a working payment flow.

    The id matters as much as the url: the caller records it on
    Invoice.stripe_payment_link_ids so a later cancellation (see
    invoicing.cancel_invoice) has something to deactivate. A Payment Link
    never expires on its own, so that id is the only way to actually close
    a link a client already has open, rather than just stopping new ones
    from being issued.
    """
    venue = invoice.booking.venue
    api_key = secret_key_for(venue)
    assert_key_belongs_to(venue, api_key)

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
        api_key=api_key,
    )
    # The ACCOUNT comes back with the id, because a link can only ever be
    # deactivated through the account that minted it -- and once there are
    # two, "whichever key is current" is not that account.
    return payment_link.url, payment_link.id, (getattr(venue, "stripe_account_id", None) or "")


def deactivate_payment_links(invoice, link_ids: list[str] | None = None) -> None:
    """Best-effort: called whenever an invoice is cancelled (see
    invoicing.cancel_invoice), which itself fires whenever a booking moves
    to a terminal status (see app.services.booking.change_status) -- a
    real incident, 2026-09-04: Sophie Mavridis still had a working card
    link after her offer on the Loft was superseded by Chanai Duncombe
    confirming the same room and night.

    One link's failure (already inactive, a transient Stripe error) must
    never stop the others from being tried, and this must never raise
    into the caller -- cancelling an invoice has to succeed even when
    Stripe is unreachable. A failure here means a link stays live and
    payable; it is logged so that is visible, not silently lost."""
    # Takes the INVOICE, not a bare list of ids, because deactivating a link
    # needs the account that minted it and only the invoice knows its venue.
    try:
        api_key = secret_key_for(invoice.booking.venue)
    except StripeNotConfigured:
        return
    for link_id in link_ids if link_ids is not None else (invoice.stripe_payment_link_ids or []):
        # Stored entries may be a bare id (written before 2026-09-12) or
        # {"id": ..., "account": ...}. A bare one is Hamilton's, which is the
        # only account that existed when it was written.
        ident = link_id.get("id") if isinstance(link_id, dict) else link_id
        if not ident:
            continue
        try:
            stripe.PaymentLink.modify(ident, active=False, api_key=api_key)
        except stripe.StripeError:
            logger.exception("Could not deactivate Stripe Payment Link %s -- it may still be payable", ident)
