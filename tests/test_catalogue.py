import datetime as dt
from decimal import Decimal

from app.services.booking import create_booking
from app.services.catalogue import get_active_items, resolve_pizza_price, resolve_price
from app.services.policy import PIZZA_LEGACY_PRICING_CUTOVER_DATE


def _booking_created_at(db, loft, created_at: dt.datetime):
    booking = create_booking(
        db,
        space_id=loft.id,
        contact_id=None,
        event_date=dt.date(2027, 1, 1),
        event_name="Catalogue Pricing Test",
        event_type=None,
        adult_count=50,
        child_count=0,
        notes=None,
        actor="test",
    )
    booking.created_at = created_at
    booking.pricing_locked_at = created_at.date()
    db.commit()
    db.refresh(booking)
    return booking


def test_get_active_items_returns_only_active(db, menu_items):
    from app.models.menu_item import MenuItemCategory

    pizza_item = menu_items["Margherita Pizza"]
    pizza_item.is_active = False
    db.commit()

    items = get_active_items(db, MenuItemCategory.pizza)
    names = {i.name for i in items}
    assert "Margherita Pizza" not in names
    assert "Vegetarian Pizza" in names


def test_pre_cutover_booking_uses_legacy_pizza_price(db, loft, menu_items):
    before_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=1), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, before_cutover)
    margherita = menu_items["Margherita Pizza"]

    assert resolve_pizza_price(margherita, booking) == Decimal("26.00")
    assert resolve_price(margherita, booking) == Decimal("26.00")


def test_post_cutover_booking_uses_current_pizza_price(db, loft, menu_items):
    after_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE + dt.timedelta(days=1), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, after_cutover)
    margherita = menu_items["Margherita Pizza"]

    assert resolve_pizza_price(margherita, booking) == Decimal("27.00")


def test_boundary_date_booking_uses_current_price_not_legacy(db, loft, menu_items):
    # created_at.date() == cutover date itself is NOT "before" the cutover,
    # so it prices at current -- the `<` in resolve_pizza_price is strict.
    on_cutover = dt.datetime.combine(PIZZA_LEGACY_PRICING_CUTOVER_DATE, dt.time(0, 0), tzinfo=dt.timezone.utc)
    booking = _booking_created_at(db, loft, on_cutover)
    margherita = menu_items["Margherita Pizza"]

    assert resolve_pizza_price(margherita, booking) == Decimal("27.00")


def test_vegetarian_pizza_on_legacy_booking_has_no_price_never_guesses(db, loft, menu_items):
    """Vegetarian Pizza is new in Master Policy v1.3 -- a pre-cutover
    booking has no legacy price defined for it at all. Must return None,
    never silently fall back to the current price or raise."""
    before_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=30), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, before_cutover)
    vegetarian = menu_items["Vegetarian Pizza"]

    assert resolve_pizza_price(vegetarian, booking) is None
    assert resolve_price(vegetarian, booking) is None


def test_platters_have_no_legacy_variant_always_current_price(db, loft, menu_items):
    before_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=30), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, before_cutover)
    grazing_platter = menu_items["Grazing Platter"]

    assert resolve_price(grazing_platter, booking) == Decimal("250.00")


# --- cake catalogue (Desserts menu, August 2026) -----------------------------


def test_cake_catalogue_has_all_nine_items(db, loft, menu_items):
    from app.models.menu_item import MenuItemCategory

    cakes = {i.name for i in get_active_items(db, MenuItemCategory.cake)}
    assert cakes == {
        "Vanilla Cake (2 Layer)", "Vanilla Cake (3 Layer)", "Vanilla Cake (4 Layer)",
        "Chocolate Cake (2 Layer)", "Chocolate Cake (3 Layer)", "Chocolate Cake (4 Layer)",
        "Chocolate Mud Cake", "White Chocolate, Vanilla & Raspberry Cake", "Tiramisu Cake",
    }


def test_cake_prices_match_the_desserts_menu(db, loft, menu_items):
    before_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=30), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, before_cutover)
    assert resolve_price(menu_items["Vanilla Cake (2 Layer)"], booking) == Decimal("80.00")
    assert resolve_price(menu_items["Vanilla Cake (3 Layer)"], booking) == Decimal("95.00")
    assert resolve_price(menu_items["Vanilla Cake (4 Layer)"], booking) == Decimal("115.00")
    assert resolve_price(menu_items["Chocolate Mud Cake"], booking) == Decimal("80.00")
    # Cakes have no legacy variant, same as platters -- a pre-cutover
    # booking still prices at current_price.


def test_dessert_platter_seeded_as_one_platter_item(db, loft, menu_items):
    assert menu_items["Dessert Platter"].current_price == Decimal("140.00")
    from app.models.menu_item import MenuItemCategory
    assert menu_items["Dessert Platter"].category == MenuItemCategory.platter
