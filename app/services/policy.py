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

# BOTH SURCHARGES COME OFF (Aaron, 2026-09-11; deadline 1 October 2026).
# They are removed differently, on purpose.
#
# THE CARD SURCHARGE IS DELETED OUTRIGHT, not sunset. It was never stored
# on an invoice (see app/models/invoice.py) -- it existed only as an
# addition made when a payment link was built -- so removing it changes no
# historical record and nothing becomes unreproducible. Dropping it before
# the deadline rather than on it costs nothing and closes the window where
# a client could be charged a fee the agreement no longer discloses. The
# previous implementation kept surcharging Amex, Diners, PayPal and BNPL,
# which the RBA ban does not cover; Aaron's instruction is that the 1.8%
# comes off, so the exempt-network branch goes with it.
#
# It also removed a latent bug worth recording: the old date gate was fed
# dt.date.today(), the container's local date, and no timezone is pinned
# anywhere in this repo. On a UTC host the ban would not have taken effect
# until mid-morning Sydney time on 1 October, so a client opening their
# invoice that morning would still have been given a surcharged payment
# link. Deleting the charge removes the gate and the bug together.
#
# THE PUBLIC-HOLIDAY SURCHARGE IS SUNSET BY EVENT DATE, not deleted,
# because it IS stored on the invoice and folded into the total. A draft
# invoice for a public holiday that has already happened must not silently
# lose the amount that event was actually surcharged the next time
# somebody saves it. No public holiday falls between this change and the
# end date -- the next one is Labour Day, 5 October 2026 -- so no event
# still to come is charged differently either way. The gate exists only so
# that a past event's figures stay reproducible.
PUBLIC_HOLIDAY_SURCHARGE_RATE = Decimal("0.10")
SURCHARGE_END_DATE = dt.date(2026, 10, 1)


def public_holiday_surcharge_applies(event_date: dt.date) -> bool:
    """Whether an event on this date still attracts the public-holiday
    surcharge. WHETHER the date is a public holiday is a separate question
    and the caller's -- see invoicing.is_public_holiday."""
    return event_date < SURCHARGE_END_DATE


# --- Everything below is sourced from the Meantime Hamilton Master Policy
# v1.3 doc (locked, August 2026) -- read in full and cross-checked against
# this module; STANDARD_DEPOSIT and the two surcharge rates above already
# matched it exactly, no drift found there. The Master Policy is now OUT OF
# DATE on both surcharges: Aaron ended them on 2026-09-11 and the card one
# no longer exists in this module at all. The doc is still the source for
# everything else below.

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

# When the balance falls due on a final invoice. Confirmed by Aaron
# 2026-09-08 as THE figure, in answer to a direct question: before this the
# final invoice was due ON the event date and nothing anywhere -- not this
# file, not the agreement's terms -- said what it should be.
#
# Deliberately NOT reusing HOLD_EXPIRY_DAYS, which is also 7 and also about
# payment: that one is how long a tentative hold runs before the DEPOSIT is
# chased or released, and is a different rule that happens to share a
# number. Two rules on one constant is how the Event Order lead time ended
# up with three different figures in four places.
FINAL_BALANCE_DUE_DAYS_BEFORE_EVENT = 7

# How far BEFORE the event an issued invoice's due date may legitimately
# sit, for reconciliation.check_invoice_due_date_predates_the_event.
#
# The due date is set once from the event date and never re-derived, while
# the invoice's own "Date of Service" column reads booking.event_date live
# -- so a postponed event leaves the two disagreeing on the client's copy.
# A deposit falls due on issue and a balance seven days before, so any
# honest gap is small; anything past a month means the due date was set for
# a date that has since moved. Deliberately generous: this reports, and a
# check that cries wolf on an ordinary deposit is one nobody reads.
INVOICE_DUE_DATE_MAX_LEAD_DAYS = 30


def final_balance_due_date(event_date: dt.date | None, *, issued_on: dt.date) -> dt.date | None:
    """When a final invoice's balance falls due.

    Seven days before the event -- floored at the issue date, because the
    wizard routinely comes back later than that. Both bookings live when
    this was written had their wizard submitted 3 and 5 days out
    (HAM-20260911-AKPSO, HAM-20260912-2R11Q), so a bare event_date - 7
    would have issued an invoice that was already overdue on the day it was
    created, and gone straight into arrears chasing for a client who had
    done nothing wrong.

    Aaron's ruling, 2026-09-08: seven days before, due on issue if that has
    already passed.

    An undated booking has no answer: an enquiry can reach the booking page
    before anybody has agreed a date, and inventing one would date an
    invoice from nothing. None means "no suggestion", and the form that
    reads it simply has no prefill -- which is what it had before this
    existed.
    """
    if event_date is None:
        return None
    return max(event_date - dt.timedelta(days=FINAL_BALANCE_DUE_DAYS_BEFORE_EVENT), issued_on)


# Master Policy doc says wizard links must "expire after the event" but
# gives no exact duration -- 21 days confirmed by Aaron.
WIZARD_TOKEN_TTL_DAYS = 21

# Grace on top of the date the client is TOLD to complete by. The resume
# email names an absolute due date (event_date - WIZARD_TRIGGER_DAYS_
# BEFORE_EVENT) while the link expired 21 days after it was created --
# two unrelated clocks. Send a wizard early, as staff do when a client
# asks months ahead, and the link dies before the date the client was
# given, with the email still naming it. A few days past the due date
# costs nothing and means the promise in the email is one the link can
# keep. See wizard.get_or_create_session, which takes the later of the two.
WIZARD_TOKEN_GRACE_DAYS_AFTER_DUE = 3


def wizard_token_expiry(event_date, *, created_at):
    """When a wizard link stops working: 21 days from issue, or a few days
    past the due date the client is told, whichever is LATER.

    An undated booking cannot have a due date, so the flat TTL stands --
    and create_session already refuses to issue a link before the date is
    confirmed, so that branch is for callers that bypass it.
    """
    import datetime as _dt

    flat = created_at + _dt.timedelta(days=WIZARD_TOKEN_TTL_DAYS)
    if event_date is None:
        return flat
    due = event_date - _dt.timedelta(days=WIZARD_TRIGGER_DAYS_BEFORE_EVENT)
    covers_the_promise = _dt.datetime.combine(
        due + _dt.timedelta(days=WIZARD_TOKEN_GRACE_DAYS_AFTER_DUE),
        _dt.time(23, 59, 59),
        tzinfo=created_at.tzinfo,
    )
    return max(flat, covers_the_promise)


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
