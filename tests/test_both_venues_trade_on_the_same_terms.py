"""Both companies trade on the same terms. Aaron's ruling, 2026-09-15.

Venue IDENTITY is on the venue row -- trading name, ABN, bank, address --
because The Entrance is a different company and a contract naming the
wrong one is a client paying the wrong entity.

Venue POLICY is NOT, and that is now a decision rather than an oversight.
Every generated agreement carries the Meantime Hamilton Master Policy v1.3
figures whatever venue it belongs to:

    STANDARD_DEPOSIT                        $500
    EVENT_ORDER_LEAD_DAYS                     14
    CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD   $20 per head
    SHORTFALL_RATE_PER_ADULT                 $50 per adult
    AV_USB_DEADLINE_DAYS_BEFORE_EVENT          2

Asked directly, with the figures in front of him and the cost of each
alternative: "identical -- nothing to build."

WHY THIS FILE EXISTS AT ALL. Until it was asked, the sharing was an
accident that looked like a decision: nobody had chosen it, it was simply
what happened when the identity work moved the columns it moved. An
accident and a ruling are indistinguishable from the code, and the next
person to notice would have re-derived the whole question -- or, worse,
"fixed" it by adding a venue column and quietly given the two companies
different contracts.

So this is the ruling, written where somebody changing it has to read it.
If a venue ever needs its own figure, these tests are what must be changed
on purpose, and the change is not a column: each of these reaches a
CLAUSE, and two of them reach money that is frozen at signature.

MINIMUM SPEND AND MINIMUM ADULTS ARE NOT IN THIS FILE. They come from the
`spaces` row and have always been per venue, which is why the Entrance's
own room can carry its own numbers without any of this changing.
"""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import seed
from app.models import Space, Venue
from app.models.document import DocumentType
from app.services import documents as documents_service, policy
from app.services.document_generation import generate_agreement_content

# The figures as ruled. Written out rather than read from policy.py, so a
# change to the constant fails HERE, where the reasoning is, instead of
# silently agreeing with itself.
AGREED_TERMS = {
    "STANDARD_DEPOSIT": Decimal("500.00"),
    "EVENT_ORDER_LEAD_DAYS": 14,
    "CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD": Decimal("20.00"),
    "SHORTFALL_RATE_PER_ADULT": Decimal("50.00"),
    "AV_USB_DEADLINE_DAYS_BEFORE_EVENT": 2,
}


@pytest.fixture()
def second_venue(db, hamilton):
    """A fully set-up second company, built the short way -- this file is
    about the TERMS, and the launch path has its own rehearsal."""
    venue = Venue(name="The Entrance", slug="entrance-terms")
    for column in seed.CLIENT_FACING_COLUMNS:
        setattr(venue, column, getattr(hamilton, column))
    venue.trading_name = "Meantime The Entrance"
    venue.legal_name = "Nice Try Events Pty Ltd"
    venue.abn = "28 647 750 892"
    venue.reference_prefix = "ENT2"
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name=seed.UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    room = Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"),
        is_bookable=True, has_per_head_shortfall_fee=True,
    )
    db.add(room)
    db.flush()
    return venue, room


@pytest.fixture()
def second_venue_booking(db, second_venue, contact):
    from app.services.booking import create_booking

    _venue, room = second_venue
    return create_booking(
        db, space_id=room.id, contact_id=contact.id, event_date=dt.date(2027, 7, 3),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Terms Test",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )


@pytest.mark.parametrize("name,expected", sorted(AGREED_TERMS.items()))
def test_the_agreed_figure_has_not_moved(name, expected):
    """The figures themselves. A change to any of these now changes BOTH
    companies' contracts, which is the whole content of the ruling."""
    actual = getattr(policy, name)

    assert actual == expected, (
        f"policy.{name} moved from {expected} to {actual}. Both venues trade on "
        "these terms (Aaron, 2026-09-15), so this changes every contract at "
        "both companies -- update this test deliberately if that is intended."
    )


def test_a_second_venues_agreement_carries_the_same_deposit(db, second_venue_booking, booking):
    """THE money one. The deposit prints in the clause, freezes onto the
    agreement as deposit_required, and is what the deposit invoice bills."""
    theirs = generate_agreement_content(second_venue_booking)
    hamiltons = generate_agreement_content(booking)

    assert theirs["deposit_required"] == hamiltons["deposit_required"]
    assert theirs["deposit_required"] == str(AGREED_TERMS["STANDARD_DEPOSIT"])


def test_a_second_venues_agreement_carries_the_same_clauses(db, second_venue_booking, booking):
    """Every figure, as it READS on the contract -- not as a constant.
    A per-venue column added without a decision would show up here, in the
    words a client signs."""
    theirs = repr(generate_agreement_content(second_venue_booking))
    hamiltons = repr(generate_agreement_content(booking))

    for fragment in (
        f"${int(AGREED_TERMS['STANDARD_DEPOSIT'])}",
        f"{AGREED_TERMS['EVENT_ORDER_LEAD_DAYS']} days",
        f"${int(AGREED_TERMS['CANCELLATION_SHORT_NOTICE_FEE_PER_HEAD'])} per head",
        f"${int(AGREED_TERMS['SHORTFALL_RATE_PER_ADULT'])} per person",
    ):
        assert fragment in theirs, (
            f"the second venue's agreement no longer says {fragment!r} -- if a "
            "venue now carries its own figure, that is a change to what a client "
            "signs and this file is where it is decided"
        )
        assert fragment in hamiltons, f"Hamilton's agreement no longer says {fragment!r}"


def test_the_deposit_invoice_bills_the_same_figure(db, second_venue_booking):
    """The other half of the deposit: what is actually charged. The invoice
    reads the agreement's frozen figure where there is one, so this proves
    the freeze and the bill agree for a second company too."""
    from app.services import invoicing

    documents_service.create_new_version(
        db, second_venue_booking, DocumentType.agreement,
        generate_agreement_content(second_venue_booking), actor="test",
    )
    invoice = invoicing.create_deposit_invoice(
        db, second_venue_booking, due_date=dt.date(2027, 6, 1), actor="test"
    )

    assert invoice.total == AGREED_TERMS["STANDARD_DEPOSIT"]


def test_the_rooms_own_figures_are_still_the_rooms(db, second_venue, second_venue_booking, loft):
    """The control, and the line the ruling does NOT cross. Minimum spend
    and minimum adults come from the space, so the second venue's room can
    differ from Hamilton's while the terms above stay shared. Without this,
    'both venues are identical' would read as a claim about everything."""
    _venue, room = second_venue

    assert room.min_food_spend != loft.min_food_spend or room.capacity != loft.capacity, (
        "the fixture gave the second venue a room identical to Hamilton's, so "
        "this proves nothing -- give it different figures"
    )
    assert second_venue_booking.agreed_min_food_spend == room.min_food_spend
    assert second_venue_booking.agreed_min_adults == room.standard_min_adults


def test_no_venue_column_carries_a_policy_figure(db):
    """The trap-setter. The day somebody adds `deposit` or `shortfall_rate`
    to the venues table, this fails and points them at the ruling -- which
    is the outcome that was missing before, when the sharing was an
    accident nobody had chosen.

    It is not a prohibition. It is a requirement to decide out loud.
    """
    policy_shaped = {
        "deposit", "shortfall", "cancellation", "lead_days", "lead_time",
        "av_deadline", "surcharge",
    }
    offenders = [
        column.name for column in Venue.__table__.columns
        if any(word in column.name.lower() for word in policy_shaped)
    ]

    assert not offenders, (
        f"venues now carries {offenders}, which looks like a per-venue policy "
        "figure. Both companies trade on the same terms by Aaron's ruling of "
        "2026-09-15 -- if that has changed, change this test and the agreement "
        "clauses together, on purpose."
    )
