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
