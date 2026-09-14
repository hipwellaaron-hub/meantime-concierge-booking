"""Idempotent seed data for Hamilton venue and its spaces.

These figures come straight from the build prompt, not a Handover document
(none was available at build time) — treat them as provisional and verify
against the real Hamilton Handover doc before relying on them for pricing
or capacity decisions.

Run with: python -m app.seed
"""

from decimal import Decimal

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Space, Venue
from app.services import policy
from app.services.stripe_integration import DEFAULT_STRIPE_WEBHOOK_SECRET_ENV

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
        # The venue screen is in this room. Stated here so a fresh database
        # and a migrated one agree -- migration e8a1c6f4b209 backfills the
        # same fact for an existing one.
        has_screen=True,
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
            # The shared endpoint verifies against this variable, and naming it here
            # is what tells webhooks._venue_mismatch that Hamilton's account is
            # the one signing there. One constant, so seed, guard and reader
            # cannot spell it three ways.
            "stripe_webhook_secret_env": DEFAULT_STRIPE_WEBHOOK_SECRET_ENV,
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


# Of the above, the ones that do not merely print blank -- they REFUSE.
# Both raises are deliberate and both are correct (a default would stamp
# the other company's letters on a reference nobody can rewrite), which is
# exactly why the gap has to be visible before the venue takes its first
# enquiry rather than as a 500 on it:
#
#   * booking.generate_reference_code raises ValueError, so the venue
#     cannot take a BOOKING at all.
#   * migration f3d9b7c1a468's invoice trigger RAISEs in Postgres, so it
#     cannot issue an INVOICE either.
HARD_BLOCK_COLUMNS = ("reference_prefix",)


def _is_blank(value) -> bool:
    """WHITESPACE IS BLANK, and the two enforcement sites already agree:
    booking.generate_reference_code does `(... or "").strip()` and refuses,
    and migration f3d9b7c1a468's trigger does `btrim(COALESCE(...))` and
    RAISEs. Until 2026-09-14 this helper did not, so a reference_prefix of
    "  " -- which is what a hand-typed row gets far more often than NULL --
    reported the venue ready while both of those refused it.

    trading_days is a list, not a string; `not []` already answers for it
    and strip() would raise.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return not value


def unfilled_columns(venue) -> list[str]:
    """Which of the above this venue has not been given."""
    return [c for c in CLIENT_FACING_COLUMNS if _is_blank(getattr(venue, c, None))]


def blocking_columns(venue) -> list[str]:
    """The subset of the venue's gaps that stop it working, not just
    printing blank."""
    return [c for c in unfilled_columns(venue) if c in HARD_BLOCK_COLUMNS]


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


def report_every_venue(db) -> list[str]:
    """A line per venue, in slug order.

    EVERY venue, not the one that was just seeded. `seed()` creates and
    returns Hamilton and only Hamilton, so reporting on its return value
    meant the second venue -- the one actually likely to be half-filled,
    because somebody typed its row in by hand -- was never looked at. The
    gaps it would have reported are blanks on a client's invoice.
    """
    return [report_gaps(v) for v in db.scalars(select(Venue).order_by(Venue.slug)).all()]


if __name__ == "__main__":
    seed()
    print("Seeded Hamilton venue and spaces.")
    with SessionLocal() as db:
        for line in report_every_venue(db):
            print(line)
