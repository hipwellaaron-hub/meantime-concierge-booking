from decimal import Decimal

from app.services.food_guidance import generate_food_guidance


def test_under_minimum_spend_states_shortfall_plainly():
    guidance = generate_food_guidance(
        subtotal=Decimal("420.00"), min_food_spend=Decimal("500.00"), platter_count=4, total_guest_count=50
    )
    assert guidance.met_minimum_spend is False
    assert guidance.shortfall == Decimal("80.00")
    assert "$80" in guidance.message
    assert "$500" in guidance.message


def test_over_minimum_but_light_on_platters_gives_advisory_range():
    # 60 guests: expected range is ceil(60/5)=12 to ceil(60/4)=15,
    # matching the brief's own tone example exactly. Only 8 platters
    # ordered -- below the low end -- so the range guidance should appear.
    guidance = generate_food_guidance(
        subtotal=Decimal("1040.00"), min_food_spend=Decimal("1000.00"), platter_count=8, total_guest_count=60
    )
    assert guidance.met_minimum_spend is True
    assert guidance.shortfall is None
    assert guidance.expected_platter_range == (12, 15)
    assert "12 to 15 platters" in guidance.message
    assert "$1,040" in guidance.message
    assert "cleared your minimum spend" in guidance.message


def test_over_minimum_and_plenty_of_platters_no_nagging():
    guidance = generate_food_guidance(
        subtotal=Decimal("1500.00"), min_food_spend=Decimal("1000.00"), platter_count=20, total_guest_count=60
    )
    assert guidance.met_minimum_spend is True
    assert "12 to 15 platters" not in guidance.message
    assert "cleared your minimum spend" in guidance.message


def test_guidance_is_advisory_only_never_raises_for_any_combination():
    # Zero platters, zero guests, huge spend, tiny spend -- nothing here
    # should ever raise. Guidance is advisory, never a blocking error.
    for subtotal in (Decimal("0.00"), Decimal("50.00"), Decimal("99999.00")):
        for guests in (0, 1, 500):
            for platters in (0, 1, 100):
                generate_food_guidance(
                    subtotal=subtotal, min_food_spend=Decimal("500.00"), platter_count=platters, total_guest_count=guests
                )


def test_zero_guests_has_no_platter_range():
    guidance = generate_food_guidance(
        subtotal=Decimal("500.00"), min_food_spend=Decimal("500.00"), platter_count=0, total_guest_count=0
    )
    assert guidance.expected_platter_range is None


def test_exactly_at_minimum_spend_counts_as_met():
    guidance = generate_food_guidance(
        subtotal=Decimal("500.00"), min_food_spend=Decimal("500.00"), platter_count=10, total_guest_count=50
    )
    assert guidance.met_minimum_spend is True
    assert guidance.shortfall is None
