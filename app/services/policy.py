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

# How long an automatic tentative hold runs before it should be chased or
# released. Deposits are due on issue, so a hold with no payment after this
# many days surfaces on the dashboard as "chase or release" -- it is never
# auto-released, only made visible. Dial to taste; nothing else hardcodes it.
HOLD_EXPIRY_DAYS = 7

# Rooms held back for the restaurant on Saturday nights, where covers earn
# more than a function would. Not a capacity or availability fact -- the
# room is genuinely empty in the calendar -- so nothing else in the system
# knows this, and an availability check will happily report it free. The
# draft gate consults this before offering a room it should not offer.
RESTAURANT_HELD_SATURDAY_EVENING = ("The Lounge",)

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
# pricing quoted at the time." 2026-05-01 confirmed by Aaron as the exact
# cutover date. Anchored to Booking.created_at (when the booking was
# MADE), not event_date, per the doc's own wording -- see
# app.services.catalogue.resolve_pizza_price.
PIZZA_LEGACY_PRICING_CUTOVER_DATE = dt.date(2026, 5, 1)

# Master Policy v1.3 §2.7 (Cancellation). Not invoked by any wizard logic
# yet -- added here only because this module is the stated single source
# of truth for every such figure, so a future cancellation flow has
# nowhere else to look. "1 month" is the doc's own wording, no exact day
# count given.
CANCELLATION_SHORT_NOTICE_THRESHOLD = dt.timedelta(days=30)
CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD = Decimal("20.00")  # in addition to the (always non-refundable) deposit

# The Event Order lead time. Master Policy v1.3 §2.2: "Final numbers must
# be confirmed 14 days prior to the event." Confirmed by Aaron 2026-09-03 as
# THE figure: the agreement's Booking Agreement clause, the Guest Numbers
# clause, the wizard trigger and the reconciliation "imminent, no Event
# Order" check all read this one constant. Before this there were three
# different figures (7, 14 and "2 weeks") in four places.
EVENT_ORDER_LEAD_DAYS = 14
WIZARD_TRIGGER_DAYS_BEFORE_EVENT = EVENT_ORDER_LEAD_DAYS

# Master Policy doc says wizard links must "expire after the event" but
# gives no exact duration -- 21 days confirmed by Aaron.
WIZARD_TOKEN_TTL_DAYS = 21


# --- Venue identity & banking -----------------------------------------------
# Confirmed directly by Aaron, 2026-08-11 -- not in the Master Policy doc,
# which doesn't cover banking details at all. These appear on every
# client-facing invoice and agreement, so they live here rather than being
# duplicated (and risking drift) across templates.
# Venue.name in the DB is just "Hamilton" (an internal slug-ish label
# distinguishing venues if Meantime ever opens a second one) -- both real
# iVvy documents header with "Meantime Hamilton" as the actual trading
# name, so that's what belongs on anything client-facing.
VENUE_TRADING_NAME = "Meantime Hamilton"
VENUE_LEGAL_NAME = "Meantime Pty Ltd"
VENUE_ABN = "36 654 270 532"
VENUE_ADDRESS = "104 Beaumont St, Hamilton NSW 2303"
# From the reference iVvy Event Order's own header line (Katrina Mentis
# 301-1) -- appears on every client-facing Event Order.
VENUE_PHONE = "(02) 40410697"

# The Loft's screen: video/slideshows must arrive on a USB flash drive at
# least this many days before the event for testing (phones and laptops
# cannot connect). Displayed as an absolute weekday date, never relative.
AV_USB_DEADLINE_DAYS_BEFORE_EVENT = 2

BANK_ACCOUNT_NAME = "Meantime Pty Ltd"
BANK_BSB = "063-519"
BANK_ACCOUNT_NUMBER = "10315591"

# Master Policy v1.3 §6.1: "Functions correspondence is signed: Aaron /
# Meantime Hamilton / meantimehamilton@gmail.com. The address
# hello@meantime.com.au is superseded for functions correspondence." A real
# signed contract was found using hello@meantime.com.au -- exactly known
# error #4 from §3.4 -- so this constant exists specifically to stop that
# recurring in generated documents.
VENUE_CONTACT_NAME = "Aaron"
VENUE_CONTACT_EMAIL = "meantimehamilton@gmail.com"
