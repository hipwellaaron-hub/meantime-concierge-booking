"""The venue-facing facts the AI layer needs, from one place.

Aaron's rule from 2026-09-03: anything new takes its venue from the
booking, and per-venue facts are looked up through one function that will
later take a venue, never through policy.VENUE_* directly. This is that
function for the drafting prompt and the house-rule validators.

Before this, the sign-off triple (Aaron / Meantime Hamilton /
meantimehamilton@gmail.com) existed three times -- policy, the validator
constants, and inside the prompt string -- and the $25 bar-tab guide
existed four times. A second venue would have needed all of them found and
changed. Now they are one profile, keyed by venue, and today's only entry
is Hamilton's, built from the policy constants so nothing changes for it.

When the venue-config layer lands (multi-venue slab 1), for_venue() reads
the venue's rows instead of this table. Its callers do not change.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.models import Booking, Venue
from app.services import policy


@dataclass(frozen=True)
class VenueProfile:
    slug: str
    trading_name: str
    locality: str  # "Hamilton, Newcastle" -- how the prompt places the venue
    contact_name: str  # who signs
    contact_email: str
    walkthrough_text: str  # the sentence the drafter must use when offering a look
    closed_days_text: str
    bar_tab_guide_per_person: Decimal

    @property
    def signature_lines(self) -> tuple[str, str, str]:
        return (self.contact_name, self.trading_name, self.contact_email)


_PROFILES: dict[str, VenueProfile] = {
    "hamilton": VenueProfile(
        slug="hamilton",
        trading_name=policy.VENUE_TRADING_NAME,
        locality="Hamilton, Newcastle",
        contact_name=policy.VENUE_CONTACT_NAME,
        contact_email=policy.VENUE_CONTACT_EMAIL,
        walkthrough_text="Wednesday through Sunday, 3 to 5pm",
        closed_days_text="closed Monday and Tuesday",
        bar_tab_guide_per_person=Decimal("25"),
    ),
}


def for_venue(venue: Venue) -> VenueProfile:
    try:
        return _PROFILES[venue.slug]
    except KeyError:
        # Fail loudly: a venue with no profile must not draft in Hamilton's
        # voice with Hamilton's sign-off. The drafting service records the
        # failure and the enquiry is untouched.
        raise LookupError(f"No AI venue profile for venue {venue.slug!r}; nothing will draft for it.")


def for_booking(booking: Booking) -> VenueProfile:
    return for_venue(booking.space.venue)


def default() -> VenueProfile:
    """Hamilton's profile, for callers that have no booking in hand (the
    validators' default, and tests). Goes away with the config layer."""
    return _PROFILES["hamilton"]
