"""Single source of truth for pricing and policy figures. Nothing else in
the codebase should hardcode a dollar amount or rate -- if a number needs
to change, it changes here, once. (A live bug already happened from an old
email template disagreeing with current policy by $20/platter; this module
exists specifically so that can't happen again.)

None of these figures come from a verified Handover doc (none was
available at build time) except where a citation is given -- treat the
rest as provisional pending confirmation against the real doc.
"""

import datetime as dt
from decimal import Decimal

# From the build prompt: "$500 deposit secures any booking, always credited
# toward minimum food spend."
STANDARD_DEPOSIT = Decimal("500.00")

# From the build prompt: "Credit card surcharge 1.8%." See
# CARD_SURCHARGE_BAN_DATE below -- this rate is going away for most cards.
CARD_SURCHARGE_RATE = Decimal("0.018")

# From the build prompt: "Public holiday surcharge 10%."
PUBLIC_HOLIDAY_SURCHARGE_RATE = Decimal("0.10")

# The RBA is banning surcharges on Visa, Mastercard and EFTPOS transactions
# Australia-wide from this date -- a real regulatory deadline confirmed via
# live research during this build (ANZ, Tyro, and the Australian Banking
# Association all corroborate 1 October 2026), not a guess. The ban does
# NOT cover Amex, Diners, PayPal, or BNPL -- those remain surchargeable.
# On/after this date, CARD_SURCHARGE_RATE must not be applied to a
# Visa/Mastercard/EFTPOS transaction. See is_card_surcharge_permitted().
CARD_SURCHARGE_BAN_DATE = dt.date(2026, 10, 1)
SURCHARGE_EXEMPT_NETWORKS = {"amex", "diners", "paypal", "bnpl"}
DEFAULT_CARD_NETWORK = "visa_mastercard_eftpos"


def is_card_surcharge_permitted(payment_date: dt.date, card_network: str = DEFAULT_CARD_NETWORK) -> bool:
    """Whether a card surcharge may legally be applied to a payment made on
    this date, on this card network."""
    if card_network in SURCHARGE_EXEMPT_NETWORKS:
        return True
    return payment_date < CARD_SURCHARGE_BAN_DATE


# --- Everything below is sourced from the Meantime Hamilton Master Policy
# v1.3 doc (locked, August 2026) -- read in full and cross-checked against
# this module; STANDARD_DEPOSIT/CARD_SURCHARGE_RATE/PUBLIC_HOLIDAY_SURCHARGE_RATE
# above already matched it exactly, no drift found there.

# Master Policy v1.3 §1.4 (Platters): "1 platter per 5 guests." Genuinely
# unresolved: live staff correspondence has used 1-per-4 (reasoning a
# platter is roughly four entrees), and the doc's own "Open Items" section
# says both figures are in circulation and flags this as not settled.
# Quote this locked figure and flag the discrepancy -- never silently
# switch to 1-per-4.
PLATTER_GUESTS_PER_PLATTER = 5

# Master Policy v1.3, minimum guests & shortfall: "$50 per adult below the
# agreed minimum." Applies only where Space.has_per_head_shortfall_fee is
# True (Loft/Mezzanine -- never the Lounge, which has no minimum at all).
# Must be computed against Booking.agreed_min_adults ONLY -- the doc is
# explicit that reading Space.standard_min_adults here is the exact bug
# class reduced minimums exist to prevent ("the client will be charged a
# shortfall they were never told about").
SHORTFALL_RATE_PER_ADULT = Decimal("50.00")

# Master Policy v1.3 §1.5: "bookings made before May 2026 honour the
# pricing quoted at the time." The doc names the month but not an exact
# day -- 2026-05-01 is this build's assumption, not a stated date; confirm
# the precise cutover with Aaron. Anchored to Booking.created_at (when the
# booking was MADE), not event_date, per the doc's own wording -- see
# app.services.catalogue.resolve_pizza_price.
PIZZA_LEGACY_PRICING_CUTOVER_DATE = dt.date(2026, 5, 1)

# Master Policy v1.3 §2.7 (Cancellation). Not invoked by any wizard logic
# yet -- added here only because this module is the stated single source
# of truth for every such figure, so a future cancellation flow has
# nowhere else to look. "1 month" is the doc's own wording, no exact day
# count given.
CANCELLATION_SHORT_NOTICE_THRESHOLD = dt.timedelta(days=30)
CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD = Decimal("20.00")  # in addition to the (always non-refundable) deposit

# Master Policy v1.3 §2.2: "Final numbers must be confirmed 14 days prior
# to the event." This is the Guided Booking Wizard's own trigger timing.
WIZARD_TRIGGER_DAYS_BEFORE_EVENT = 14

# Not stated in the Master Policy doc -- it says wizard links must "expire
# after the event" but gives no exact duration. 21 days comfortably covers
# a slow-to-respond client without leaving a link live indefinitely past
# the event; this build's assumption, confirm with Aaron.
WIZARD_TOKEN_TTL_DAYS = 21
