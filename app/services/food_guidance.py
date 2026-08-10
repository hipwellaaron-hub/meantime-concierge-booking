"""Food-step quantity guidance: advisory copy in Aaron's warm hospitality
voice, never a blocking error. Two concepts kept deliberately separate,
per the Master Policy doc -- they're easy to conflate and mean different
things:

- Minimum food SPEND: a dollar figure, set per space (see Space.min_food_spend).
- Platter ratio: a serving-quantity guide, unrelated to whether the spend
  minimum is met. A client can clear their minimum spend while still
  ordering too little food for their guest count -- this module says so
  plainly, without implying they've done anything wrong.

The platter ratio itself is an open policy question (Master Policy v1.3
§10.1: locked policy says 1-per-5, live staff practice has used 1-per-4).
Rather than silently picking one, the guidance quotes a range bounded by
both figures -- e.g. 60 guests -> 12 platters (1-per-5) to 15 platters
(1-per-4) -- so it stays useful without resolving an unsettled question.

This is deterministic templated copy, not a live model call: the tone
example in the brief is precise and arithmetic-driven, and near-zero
running cost plus fully predictable/testable output both favor a
template over an LLM call for something that just needs correct numbers
phrased warmly.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from app.services.policy import PLATTER_GUESTS_PER_PLATTER

# The live-practice figure from Master Policy v1.3 §10.1 -- not the locked
# policy value, only used as the *other* end of the advisory range.
_LIVE_PRACTICE_GUESTS_PER_PLATTER = 4


@dataclass
class FoodGuidance:
    subtotal: Decimal
    min_food_spend: Decimal
    met_minimum_spend: bool
    shortfall: Decimal | None  # None once the minimum is met
    expected_platter_range: tuple[int, int] | None  # None if there are no guests yet to guide against
    message: str


def _format_money(value: Decimal) -> str:
    return f"${value:,.2f}".rstrip("0").rstrip(".") if value == value.to_integral_value() else f"${value:,.2f}"


def generate_food_guidance(
    *, subtotal: Decimal, min_food_spend: Decimal, platter_count: int, total_guest_count: int
) -> FoodGuidance:
    met_minimum = subtotal >= min_food_spend
    shortfall = None if met_minimum else (min_food_spend - subtotal)

    platter_range = None
    if total_guest_count > 0:
        low = math.ceil(total_guest_count / PLATTER_GUESTS_PER_PLATTER)
        high = math.ceil(total_guest_count / _LIVE_PRACTICE_GUESTS_PER_PLATTER)
        platter_range = (min(low, high), max(low, high))

    if not met_minimum:
        message = (
            f"That comes to {_format_money(subtotal)} so far, "
            f"{_format_money(shortfall)} short of the {_format_money(min_food_spend)} minimum food spend. "
            "No rush -- keep adding until you get there, or let us know if you'd like a hand."
        )
    elif platter_range is not None and platter_count < platter_range[0]:
        message = (
            f"That comes to {_format_money(subtotal)}, so you've cleared your minimum spend. "
            f"Worth knowing that most functions your size take {platter_range[0]} to {platter_range[1]} platters, "
            "so if you'd like a bit more on the table, there's room to add without much extra cost."
        )
    else:
        message = f"That comes to {_format_money(subtotal)}, so you've cleared your minimum spend."

    return FoodGuidance(
        subtotal=subtotal,
        min_food_spend=min_food_spend,
        met_minimum_spend=met_minimum,
        shortfall=shortfall,
        expected_platter_range=platter_range,
        message=message,
    )
