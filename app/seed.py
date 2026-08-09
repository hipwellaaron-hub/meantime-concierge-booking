"""Idempotent seed data for Hamilton venue and its spaces.

These figures come straight from the build prompt, not a Handover document
(none was available at build time) — treat them as provisional and verify
against the real Hamilton Handover doc before relying on them for pricing
or capacity decisions.

Run with: python -m app.seed
"""

from decimal import Decimal

from app.database import SessionLocal
from app.models import Space, Venue

HAMILTON_SLUG = "hamilton"

# Holds bookings (e.g. from a migration import) whose real space hasn't
# been assigned yet. Never bookable -- see Space.is_bookable and
# app/services/ivvy_import.py.
UNASSIGNED_SPACE_NAME = "Unassigned (pending triage)"

HAMILTON_SPACES = [
    dict(
        name="The Loft",
        capacity=100,
        min_food_spend=Decimal("1000.00"),
        standard_min_adults=60,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    ),
    dict(
        name="The Mezzanine",
        capacity=60,
        min_food_spend=Decimal("500.00"),
        standard_min_adults=40,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    ),
    dict(
        name="The Lounge",
        capacity=35,
        min_food_spend=Decimal("500.00"),
        # No standard minimum adult count was given for the Lounge, and it
        # never charges a per-head shortfall fee anyway (see below), so 0
        # means "not enforced" rather than an actual observed figure.
        standard_min_adults=0,
        wheelchair_accessible=True,
        has_per_head_shortfall_fee=False,
    ),
    dict(
        name=UNASSIGNED_SPACE_NAME,
        capacity=0,
        min_food_spend=Decimal("0.00"),
        standard_min_adults=0,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=False,
        is_bookable=False,
    ),
]


def seed(db=None) -> Venue:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug=HAMILTON_SLUG).one_or_none()
        if venue is None:
            venue = Venue(name="Hamilton", slug=HAMILTON_SLUG)
            db.add(venue)
            db.flush()

        existing_names = {s.name for s in db.query(Space).filter_by(venue_id=venue.id)}
        for space_kwargs in HAMILTON_SPACES:
            if space_kwargs["name"] in existing_names:
                continue
            db.add(Space(venue_id=venue.id, **space_kwargs))

        db.commit()
        db.refresh(venue)
        return venue
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":
    seed()
    print("Seeded Hamilton venue and spaces.")
