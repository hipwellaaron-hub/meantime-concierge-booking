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


def test_cake_catalogue_offers_only_the_flat_price_cakes(db, loft, menu_items):
    """Hamilton's live cake list is three flat-$80 cakes (per Aaron,
    27 Aug 2026). The layered Vanilla/Chocolate variants are retired --
    kept in the DB inactive so existing orders resolve, never offered."""
    from app.models.menu_item import MenuItemCategory

    cakes = {i.name for i in get_active_items(db, MenuItemCategory.cake)}
    assert cakes == {
        "Chocolate Mud Cake", "White Chocolate, Vanilla & Raspberry Cake", "Tiramisu Cake",
    }


def test_cake_prices_match_the_desserts_menu(db, loft, menu_items):
    before_cutover = dt.datetime.combine(
        PIZZA_LEGACY_PRICING_CUTOVER_DATE - dt.timedelta(days=30), dt.time(9, 0), tzinfo=dt.timezone.utc
    )
    booking = _booking_created_at(db, loft, before_cutover)
    assert resolve_price(menu_items["Chocolate Mud Cake"], booking) == Decimal("80.00")
    assert resolve_price(menu_items["White Chocolate, Vanilla & Raspberry Cake"], booking) == Decimal("80.00")
    assert resolve_price(menu_items["Tiramisu Cake"], booking) == Decimal("80.00")
    # Cakes have no legacy variant, same as platters -- a pre-cutover
    # booking still prices at current_price.


def test_retired_items_resolve_for_existing_orders_but_are_not_offered(db, loft, menu_items):
    """A retired item keeps its identity and quoted price for orders that
    already reference it -- retirement only means "no longer offered"."""
    from app.models.menu_item import MenuItemCategory
    from app.services.catalogue import get_by_id, get_by_id_any

    retired_cake = menu_items["Vanilla Cake (3 Layer)"]
    dessert_platter = menu_items["Dessert Platter"]

    assert retired_cake.is_active is False
    assert dessert_platter.is_active is False
    assert dessert_platter.category == MenuItemCategory.dessert

    # Not offered for new selection...
    assert get_by_id(db, retired_cake.id) is None
    assert get_by_id(db, dessert_platter.id) is None
    # ...but an existing order still resolves at the quoted price.
    booking = _booking_created_at(db, loft, dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc))
    assert resolve_price(get_by_id_any(db, retired_cake.id), booking) == Decimal("95.00")
    assert resolve_price(get_by_id_any(db, dessert_platter.id), booking) == Decimal("140.00")


def test_hot_and_cold_dessert_platters_offered_at_100(db, loft, menu_items):
    from app.models.menu_item import MenuItemCategory

    desserts = {i.name: i for i in get_active_items(db, MenuItemCategory.dessert)}
    assert set(desserts) == {"Hot Dessert Platter", "Cold Dessert Platter"}
    assert desserts["Hot Dessert Platter"].current_price == Decimal("100.00")
    assert desserts["Cold Dessert Platter"].current_price == Decimal("100.00")


def test_shoestring_fries_is_a_side(db, loft, menu_items):
    from app.models.menu_item import MenuItemCategory

    assert menu_items["Shoestring Fries"].category == MenuItemCategory.side
    assert menu_items["Shoestring Fries"].is_active is True
