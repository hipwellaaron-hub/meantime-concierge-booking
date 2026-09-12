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
from app.services import policy

HAMILTON_SLUG = "hamilton"

# Holds bookings (e.g. from a migration import) whose real space hasn't
# been assigned yet. Never bookable -- see Space.is_bookable and
# app/services/ivvy_import.py.
UNASSIGNED_SPACE_NAME = "Unassigned (pending triage)"

HAMILTON_SPACES = [
    dict(
        name="The Loft",
        capacity=130,
        min_food_spend=Decimal("1000.00"),
        standard_min_adults=60,
        wheelchair_accessible=False,
        has_per_head_shortfall_fee=True,
    ),
    dict(
        name="The Mezzanine",
        capacity=70,
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

        # Identity, from the same values migration e1b6a44c7f83 backfills.
        # `name` stays the internal label ("Hamilton"); `trading_name` is
        # what a client sees.
        #
        # FILL ONLY WHAT IS EMPTY. This runs on every deploy (preDeploy), and
        # the client-facing renders now read these COLUMNS rather than the
        # constants -- so a plain assignment would silently revert a bank
        # account or an ABN corrected in the database, on the next deploy,
        # with nothing in the log but "Seeded Hamilton venue and spaces"
        # (review, 2026-09-12). A blank field is an unfinished setup and
        # gets a value; a filled one is somebody's answer and is left alone.
        #
        # Seeding the same literals as the migration is deliberate: a fresh
        # database and a migrated one have to agree, and reading policy.py
        # rather than repeating them keeps the two copies from drifting
        # while the constants are still there for the email sender.
        defaults = {
            "trading_name": policy.VENUE_TRADING_NAME,
            "legal_name": policy.VENUE_LEGAL_NAME,
            "abn": policy.VENUE_ABN,
            "address": policy.VENUE_ADDRESS,
            "phone": policy.VENUE_PHONE,
            "contact_name": policy.VENUE_CONTACT_NAME,
            "contact_email": policy.VENUE_CONTACT_EMAIL,
            "bank_account_name": policy.BANK_ACCOUNT_NAME,
            "bank_bsb": policy.BANK_BSB,
            "bank_account_number": policy.BANK_ACCOUNT_NUMBER,
            "reference_prefix": "HAM",
            # Wednesday through Sunday: Python weekday numbers, Monday=0.
            # Matches migration a4e7b2f9c105's backfill exactly.
            "trading_days": [2, 3, 4, 5, 6],
            "stripe_secret_key_env": "STRIPE_SECRET_KEY",
            "stripe_webhook_secret_env": "STRIPE_WEBHOOK_SECRET",
        }
        for field, value in defaults.items():
            if not getattr(venue, field, None):
                setattr(venue, field, value)

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


# Columns a CLIENT reads. A blank here is a blank on an invoice, an
# agreement or an Event Order -- venue_identity supplies no fallback on
# purpose, so nothing substitutes another company's details. That is the
# right trade only if somebody finds out, and until 2026-09-12 preDeploy's
# entire output was "Seeded Hamilton venue and spaces".
#
# stripe_account_id is deliberately NOT here: it is expected to be NULL
# until somebody arms the credential guard by hand, which is its own
# numbered step in docs/stripe-go-live-checklist.md.
CLIENT_FACING_COLUMNS = (
    "trading_name", "legal_name", "abn", "address", "phone",
    "contact_name", "contact_email",
    "bank_account_name", "bank_bsb", "bank_account_number",
    "reference_prefix", "trading_days",
    "stripe_secret_key_env", "stripe_webhook_secret_env",
)


def unfilled_columns(venue) -> list[str]:
    """Which of the above this venue has not been given."""
    return [c for c in CLIENT_FACING_COLUMNS if not getattr(venue, c, None)]


def report_gaps(venue) -> str:
    """One line for the deploy log, naming the venue and what is missing."""
    label = venue.trading_name or venue.name
    missing = unfilled_columns(venue)
    if not missing:
        return f"{label}: every client-facing column is filled."
    return (
        f"{label}: WARNING -- {len(missing)} client-facing column(s) unfilled and no fallback "
        f"exists, so these print BLANK on client documents: {', '.join(missing)}"
    )


if __name__ == "__main__":
    seeded = seed()
    print("Seeded Hamilton venue and spaces.")
    print(report_gaps(seeded))
