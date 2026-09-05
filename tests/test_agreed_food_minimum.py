"""One agreed food minimum, read once, used everywhere it is consumed.

The bug this closes reached a client: HAM-20261024-M1QZQ (Liz Kalaf, the
Loft, Friday 23 October 2026) was sent an agreement showing $1,000 in the
header summary and $500 in the Minimum Spend clause, with a covering note
telling her to ignore the header. The header rendered
Space.min_food_spend; the clause had been hand-edited to the figure
actually agreed. Two figures for one contractual term, on a document with
a signature block.

There was nowhere to record the agreed figure, so there was nothing for
the header to read. Booking.agreed_min_food_spend is that place, and it
mirrors agreed_min_adults deliberately: NOT NULL, seeded from the space,
so no consumer can reach past it to the space default and get a different
answer.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.models.booking import MinReductionReasonCode
from app.services import document_generation
from app.services.booking import (
    create_booking,
    set_agreed_food_minimum,
    set_agreed_minimum,
    set_bar_credit,
)


def _liz(db, loft, contact):
    """Her real booking, as data rather than a hand-edited document."""
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 10, 23),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Liz Kalaf",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=Decimal("500.00"),
        reason=MinReductionReasonCode.friday_incentive, actor="staff:test",
    )
    set_bar_credit(db, booking, bar_credit=Decimal("250.00"), actor="staff:test")
    return booking


# --- the column itself -------------------------------------------------


def test_a_new_booking_takes_the_spaces_food_minimum(db, loft, contact):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 10, 23),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Standard",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    assert booking.agreed_min_food_spend == loft.min_food_spend
    assert booking.agreed_min_food_spend_reason is None
    assert booking.bar_credit == Decimal("0.00")


def test_a_waived_minimum_of_zero_is_stored_and_not_treated_as_unset(db, loft, contact):
    # "A waived spend is $0, not null." Nothing may test this truthily.
    booking = _liz(db, loft, contact)
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=Decimal("0.00"),
        reason=MinReductionReasonCode.aaron_discretion, actor="staff:test",
    )
    assert booking.agreed_min_food_spend == Decimal("0.00")
    assert booking.agreed_min_food_spend is not None
    assert booking.agreed_min_food_spend_reason == MinReductionReasonCode.aaron_discretion


def test_a_reason_is_required_when_the_figure_differs_from_the_space(db, loft, contact):
    booking = _liz(db, loft, contact)
    with pytest.raises(ValueError, match="reason is required"):
        set_agreed_food_minimum(
            db, booking, agreed_min_food_spend=Decimal("750.00"), reason=None, actor="staff:test"
        )


def test_resetting_to_the_space_standard_clears_a_stale_reason(db, loft, contact):
    booking = _liz(db, loft, contact)
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=loft.min_food_spend, reason=None, actor="staff:test"
    )
    assert booking.agreed_min_food_spend_reason is None


def test_the_two_minimums_keep_independent_reasons(db, loft, contact):
    # A Friday booking routinely has a reduced spend at the standard guest
    # minimum; one shared reason column would overwrite the other's.
    booking = _liz(db, loft, contact)
    set_agreed_minimum(
        db, booking, agreed_min_adults=40, reason=MinReductionReasonCode.weekend_gap, actor="staff:test"
    )
    assert booking.agreed_min_reduction_reason == MinReductionReasonCode.weekend_gap
    assert booking.agreed_min_food_spend_reason == MinReductionReasonCode.friday_incentive


def test_negative_values_are_refused(db, loft, contact):
    booking = _liz(db, loft, contact)
    with pytest.raises(ValueError, match="cannot be negative"):
        set_agreed_food_minimum(
            db, booking, agreed_min_food_spend=Decimal("-1"),
            reason=MinReductionReasonCode.aaron_discretion, actor="staff:test",
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        set_bar_credit(db, booking, bar_credit=Decimal("-1"), actor="staff:test")


def test_every_change_is_recorded_in_the_audit_trail(db, loft, contact):
    booking = _liz(db, loft, contact)
    changed = {e.field_name for e in booking.events if e.event_type == "field_changed"}
    assert "agreed_min_food_spend" in changed
    assert "agreed_min_food_spend_reason" in changed
    assert "bar_credit" in changed


# --- the agreement: the document that went out wrong -------------------


def test_the_header_and_the_clause_state_the_same_figure(db, loft, contact):
    """The actual defect. The Loft's standard is $1,000; Liz agreed $500."""
    assert loft.min_food_spend == Decimal("1000.00"), "fixture assumption: Loft standard is $1,000"
    booking = _liz(db, loft, contact)
    content = document_generation.generate_agreement_content(booking)

    # Header summary -- was rendering the space default.
    assert content["min_food_spend"] == "500.00"

    # Minimum Spend clause -- was only right because it was hand-edited.
    clause = next(s for s in content["terms_sections"] if s["heading"] == "Minimum Spend")
    assert "$500 minimum spend" in clause["body"]
    assert "$1,000" not in clause["body"]

    # And the space default appears nowhere in the document at all.
    assert "1,000" not in content["terms_text"]


def test_the_bar_credit_prints_on_the_agreement(db, loft, contact):
    booking = _liz(db, loft, contact)
    content = document_generation.generate_agreement_content(booking)
    assert content["bar_credit"] == "250.00"
    clause = next(s for s in content["terms_sections"] if s["heading"] == "Minimum Spend")
    assert "$250 bar credit" in clause["body"]


def test_a_waived_minimum_still_carries_its_bar_credit(db, loft, contact):
    # No Minimum Spend clause for it to ride on, so it gets its own.
    booking = _liz(db, loft, contact)
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=Decimal("0"),
        reason=MinReductionReasonCode.aaron_discretion, actor="staff:test",
    )
    content = document_generation.generate_agreement_content(booking)
    headings = [s["heading"] for s in content["terms_sections"]]
    assert "Minimum Spend" not in headings
    assert "Bar Credit" in headings
    assert "$250 bar credit" in content["terms_text"]


def test_a_standard_booking_still_reads_the_space_figure(db, loft, contact):
    # Nothing changes for a booking that never negotiated: the seeded
    # value IS the space standard, so existing agreements are unaffected.
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 10, 23),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Standard",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    content = document_generation.generate_agreement_content(booking)
    assert content["min_food_spend"] == str(loft.min_food_spend)
    clause = next(s for s in content["terms_sections"] if s["heading"] == "Minimum Spend")
    assert "$1,000 minimum spend" in clause["body"]


# --- the other consumers ----------------------------------------------


def test_the_wizard_measures_the_client_against_the_agreed_figure(db, loft, contact):
    from app.services.food_guidance import generate_food_guidance
    booking = _liz(db, loft, contact)
    guidance = generate_food_guidance(
        subtotal=Decimal("500.00"), min_food_spend=booking.agreed_min_food_spend,
        platter_count=5, total_guest_count=50,
    )
    # She has met her $500 minimum. Against the space default she would be
    # told she was $500 short of a figure she was never given.
    assert guidance.met_minimum_spend is True
    assert guidance.shortfall is None


def test_the_event_order_carries_the_credit_into_the_bar_structure(db, loft, contact):
    booking = _liz(db, loft, contact)
    content = document_generation.generate_beo_content(booking, [], bar_structure="Bar tab on the night")
    assert content["bar_credit"] == "250.00"
    assert "$250 bar credit" in content["bar_structure"]
    assert "Bar tab on the night" in content["bar_structure"]


def test_no_bar_credit_leaves_the_bar_structure_untouched(db, loft, contact):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 10, 23),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="No credit",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    content = document_generation.generate_beo_content(booking, [], bar_structure="Bar tab on the night")
    assert content["bar_structure"] == "Bar tab on the night"


# --- triage must carry the food minimum across, like the guest one ------
#
# Shipped broken and caught by review hours later: every enquiry is created
# on the non-bookable "Unassigned (pending triage)" placeholder, whose
# min_food_spend is $0, so agreed_min_food_spend seeded to $0. Triage moved
# agreed_min_adults but not this, and since a $0 minimum correctly emits no
# clause, the client's agreement had no minimum in it at all.


def test_triaging_an_enquiry_into_a_real_room_brings_the_food_minimum_with_it(
    db, hamilton, loft, unassigned_space, contact
):
    from app.services.booking import assign_space_and_time
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=contact.id, event_date=dt.date(2026, 12, 12),
        start_time=None, end_time=None, event_name="Web enquiry", event_type="birthday",
        adult_count=70, child_count=0, notes=None, actor="web",
    )
    assert booking.agreed_min_food_spend == Decimal("0.00"), "placeholder seeds zero"

    assign_space_and_time(
        db, booking, space_id=loft.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0), actor="staff:test"
    )
    db.refresh(booking)

    assert booking.agreed_min_food_spend == loft.min_food_spend
    content = document_generation.generate_agreement_content(booking)
    headings = [s["heading"] for s in content["terms_sections"]]
    assert "Minimum Spend" in headings, "a triaged Loft booking must carry the Loft's minimum"
    assert content["min_food_spend"] == str(loft.min_food_spend)


def test_triage_does_not_overwrite_a_deliberately_agreed_food_minimum(
    db, hamilton, loft, mezzanine, unassigned_space, contact
):
    # A recorded reason means a human chose the figure; moving rooms must
    # not silently undo that.
    booking = create_booking(
        db, space_id=mezzanine.id, contact_id=contact.id, event_date=dt.date(2026, 12, 12),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Negotiated",
        event_type="birthday", adult_count=70, child_count=0, notes=None, actor="staff:test",
    )
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=Decimal("300.00"),
        reason=MinReductionReasonCode.friday_incentive, actor="staff:test",
    )
    from app.services.booking import assign_space_and_time
    assign_space_and_time(
        db, booking, space_id=loft.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0), actor="staff:test"
    )
    db.refresh(booking)
    assert booking.agreed_min_food_spend == Decimal("300.00")
    assert booking.agreed_min_food_spend_reason == MinReductionReasonCode.friday_incentive
