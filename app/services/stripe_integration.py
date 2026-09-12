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


# The one venue whose row may still be blank and still mean STRIPE_SECRET_KEY.
# Hamilton was taking card payments before venues had a column to name their
# own key in, so its row can legitimately be NULL during the rollback window
# and the process-wide variable is genuinely its key.
#
# NAMED, not inferred from absence. "No key named" used to fall through for
# ANY venue, on the reasoning that the only un-migrated venue is Hamilton --
# which stops being true the moment a second row exists, and a second row
# with this column NULL is exactly what the setup sequence produces.
LEGACY_STRIPE_VENUE_SLUG = "hamilton"


def secret_key_for(venue) -> str:
    """This venue's Stripe secret, by the ENVIRONMENT VARIABLE NAME stored on
    the venue row -- never a key stored in the database.

    No fallback to STRIPE_SECRET_KEY when the venue names a variable that is
    not set: falling back would mint the link in Hamilton's account for
    whichever venue asked, which is the exact failure this exists to stop.

    And no fallback when the venue names NOTHING either, unless that venue
    is Hamilton. A second company's invoice minting a link inside Meantime
    Pty Ltd's Stripe account is not a visible error -- that account signs
    its own completion event, so it verifies, and it records as a successful
    payment against an invoice that has not been paid. Refusing costs a
    card button on a venue nobody has finished setting up.
    """
    name = getattr(venue, "stripe_secret_key_env", None)
    if not name:
        slug = getattr(venue, "slug", None)
        if slug != LEGACY_STRIPE_VENUE_SLUG:
            raise StripeNotConfigured(
                f"venue {slug!r} names no Stripe key environment variable "
                "(venues.stripe_secret_key_env is empty), and there is no key to fall "
                "back to -- another venue's key would take this venue's money into "
                "another company's account"
            )
        name = "STRIPE_SECRET_KEY"
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
    # getattr, NOT .get(): stripe 15.4.0's StripeObject is not a dict
    # subclass and defines no .get(), so .get("id") raises AttributeError on
    # a REAL Stripe response -- and AttributeError is caught by none of the
    # three handlers on the invoice page, so a healthy, correctly-keyed
    # account 500s the client's own invoice. The tests missed it because a
    # dict stand-in for the account cannot fail that way (review, 2026-09-12).
    actual = getattr(account, "id", None)
    if actual != expected:
        raise StripeVenueMismatch(
            f"the resolved Stripe key belongs to account {actual!r}, but "
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


def _mode_of(key: str | None) -> StripeMode:
    if not key:
        return StripeMode.not_configured
    if key.startswith(("sk_test_", "rk_test_")):
        return StripeMode.test
    return StripeMode.live


def mode_for(venue) -> StripeMode:
    """Live/test for THIS venue's own key.

    The badge this feeds is on every admin page, and every admin page is
    now scoped to a venue -- so a process-wide answer there says "Stripe
    live" over a venue whose links charge nothing, or "no real charges"
    over a venue whose links charge real cards. Two companies, two keys,
    and the badge exists precisely to stop somebody assuming which.

    A venue with no key of its own reports not_configured rather than
    borrowing the answer from whichever key the process happens to hold --
    the same rule as secret_key_for, for the same reason.
    """
    if venue is None:
        return get_mode()
    try:
        return _mode_of(secret_key_for(venue))
    except StripeNotConfigured:
        return StripeMode.not_configured


def get_mode() -> StripeMode:
    """The PROCESS-wide key's mode, for a page with no venue in scope --
    the venue chooser and the login. A venue-scoped page asks mode_for().

    Derived from the secret key's own prefix, never from a separate
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
    return _mode_of(STRIPE_SECRET_KEY)


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


def _venue_that_minted(invoice, account: str):
    """Which venue's key can close a link that was minted in `account`.

    No account recorded means the entry predates the account being stored,
    and create_payment_link has always minted with the INVOICE's own venue's
    key -- so that venue is the answer, not a guess.

    A recorded account that no venue row claims is the dangerous one: the
    venue's key has been repointed since, and the link can no longer be
    closed from here at all. None, so the caller says so out loud.
    """
    venue = invoice.booking.venue
    if not account:
        return venue
    if account == (getattr(venue, "stripe_account_id", None) or ""):
        return venue

    from sqlalchemy import select
    from sqlalchemy.orm import object_session

    from app.models import Venue

    session = object_session(invoice)
    if session is None:
        return None
    return session.scalars(select(Venue).where(Venue.stripe_account_id == account)).one_or_none()


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
    #
    # PER LINK, not per invoice. This used to resolve one key up front and
    # present it for every link -- ignoring the account recorded beside each
    # id, which exists for exactly this. A link minted in another account
    # then got "No such payment link", which is caught, logged, and leaves
    # the link live and payable.
    for link_id in link_ids if link_ids is not None else (invoice.stripe_payment_link_ids or []):
        # Stored entries may be a bare id (written before 2026-09-12) or
        # {"id": ..., "account": ...}.
        ident = link_id.get("id") if isinstance(link_id, dict) else link_id
        if not ident:
            continue
        account = (link_id.get("account") or "") if isinstance(link_id, dict) else ""
        venue = _venue_that_minted(invoice, account)
        if venue is None:
            # Refused rather than attempted with a key we already know is
            # wrong: "could not deactivate" would read as Stripe being
            # flaky, when the truth is there is no key here that can close
            # this link and somebody has to do it in the dashboard.
            logger.error(
                "Stripe Payment Link %s was minted in account %r and no venue row claims that "
                "account, so no key here can close it -- THE LINK IS STILL PAYABLE and must be "
                "deactivated in the Stripe dashboard by hand",
                ident, account,
            )
            continue
        try:
            api_key = secret_key_for(venue)
        except StripeNotConfigured:
            logger.error(
                "Stripe Payment Link %s belongs to venue %r, which has no key configured -- "
                "THE LINK IS STILL PAYABLE",
                ident, getattr(venue, "slug", None),
            )
            continue
        try:
            stripe.PaymentLink.modify(ident, active=False, api_key=api_key)
        except stripe.StripeError:
            logger.exception("Could not deactivate Stripe Payment Link %s -- it may still be payable", ident)
