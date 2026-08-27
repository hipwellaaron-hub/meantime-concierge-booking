"""Idempotent seed of the function-catering catalogue (platters, pizzas,
cakes). Platters/pizzas are sourced verbatim from the Meantime Hamilton
Master Policy v1.3 doc (locked, August 2026), sections 1.4 and 1.5. Cakes
and the dessert platter are sourced from the Desserts menu doc (August
2026) -- the in-house cake catalogue's first machine-readable version
(see app.services.wizard's extras step, which reads MenuItemCategory.cake
rows for the cake picker).

Legacy prices are the pre-May-2026 figures the doc gives for pizzas only
-- platters and cakes have no legacy variant (see app.services.catalogue).
Vegetarian Pizza has no legacy price: it's a new item introduced in v1.3,
so it never had a pre-cutover price to honour.

Vanilla Cake and Chocolate Cake are each offered at three sizes with
different prices (2/3/4 layer) -- modelled as three separate rows per
flavour rather than adding a tiering concept to MenuItem, since every
other price on this table is already a flat per-item figure.

Run with: python -m app.seed_catalogue
"""

from decimal import Decimal

from app.database import SessionLocal
from app.models import MenuItem
from app.models.menu_item import MenuItemCategory

# (category, name, current_price, legacy_price, is_active)
#
# Retired rows stay in this list, inactive, on purpose: existing bookings
# reference them by id and must keep resolving to the name and price that
# was quoted (see app.services.catalogue.get_by_id_any); they simply stop
# being offered for new selection. Retired 27 Aug 2026 per Aaron: the
# layered Vanilla/Chocolate cakes (Hamilton's cakes are now a flat $80
# each) and the $140 mixed Dessert Platter (replaced by the Hot/Cold
# Dessert Platters at $100).
CATALOGUE_ITEMS = [
    (MenuItemCategory.platter, "Pork Belly Bites", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Chicken Tender Skewers", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Salt & Pepper Squid", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Mushroom & Feta Arancini", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Crispy Popcorn Halloumi", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Pork & Fennel Meatballs", Decimal("100.00"), None, True),
    (MenuItemCategory.platter, "Ricotta & Sun-dried Tomato Stuffed Mushrooms", Decimal("80.00"), None, True),
    (MenuItemCategory.platter, "Grazing Platter", Decimal("250.00"), None, True),
    # Garlic Bread, Sticky Chilli Tofu, Corn Ribs, Wedges.
    (MenuItemCategory.platter, "Custom Vegan Platter", Decimal("140.00"), None, True),
    (MenuItemCategory.side, "Shoestring Fries", Decimal("15.00"), None, True),
    # Retired: the old mixed selection, replaced by the two platters below.
    (MenuItemCategory.dessert, "Dessert Platter", Decimal("140.00"), None, False),
    # Brownie and sticky date pudding with cream -- five of each, ten pieces.
    (MenuItemCategory.dessert, "Hot Dessert Platter", Decimal("100.00"), None, True),
    # Five different cold desserts, two of each, ten pieces.
    (MenuItemCategory.dessert, "Cold Dessert Platter", Decimal("100.00"), None, True),
    (MenuItemCategory.pizza, "Margherita Pizza", Decimal("27.00"), Decimal("26.00"), True),
    (MenuItemCategory.pizza, "Vegetarian Pizza", Decimal("30.00"), None, True),  # new item in v1.3, no legacy price
    (MenuItemCategory.pizza, "Prosciutto & Pear", Decimal("34.00"), Decimal("30.00"), True),
    (MenuItemCategory.pizza, "Spiced Lamb", Decimal("34.00"), Decimal("30.00"), True),
    (MenuItemCategory.pizza, "Calabrese", Decimal("34.00"), Decimal("30.00"), True),
    (MenuItemCategory.cake, "Vanilla Cake (2 Layer)", Decimal("80.00"), None, False),
    (MenuItemCategory.cake, "Vanilla Cake (3 Layer)", Decimal("95.00"), None, False),
    (MenuItemCategory.cake, "Vanilla Cake (4 Layer)", Decimal("115.00"), None, False),
    (MenuItemCategory.cake, "Chocolate Cake (2 Layer)", Decimal("80.00"), None, False),
    (MenuItemCategory.cake, "Chocolate Cake (3 Layer)", Decimal("95.00"), None, False),
    (MenuItemCategory.cake, "Chocolate Cake (4 Layer)", Decimal("115.00"), None, False),
    (MenuItemCategory.cake, "Chocolate Mud Cake", Decimal("80.00"), None, True),
    (MenuItemCategory.cake, "White Chocolate, Vanilla & Raspberry Cake", Decimal("80.00"), None, True),
    (MenuItemCategory.cake, "Tiramisu Cake", Decimal("80.00"), None, True),
]


# Marker codes as published on the live website menu (screenshots,
# 28 Aug 2026), mapped to this catalogue's item names. ONLY confirmed
# items appear here -- an item absent from this dict seeds with
# dietary_markers=NULL, meaning "not yet confirmed", never guessed.
# [] means confirmed to carry no marker.
DIETARY_MARKERS = {
    "Shoestring Fries": ["V", "DF"],       # website: "Fries"
    "Crispy Popcorn Halloumi": ["V"],      # website: "Popcorn Halloumi"
    "Salt & Pepper Squid": ["DFA"],
    "Pork Belly Bites": ["DF"],            # website: "BBQ Pork Belly Bites"
    "Margherita Pizza": ["V"],             # website: "Classic Margherita"
    "Prosciutto & Pear": [],
    "Spiced Lamb": [],
    "Calabrese": [],
    "Custom Vegan Platter": ["VG"],
}


def seed(db=None) -> int:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        # Keyed on name alone (not category+name): phase16 recategorized
        # Shoestring Fries and Dessert Platter via migration, and a
        # category-qualified check would re-insert a duplicate row beside
        # each migrated one.
        existing = {item.name for item in db.query(MenuItem)}
        added = 0
        for category, name, current_price, legacy_price, is_active in CATALOGUE_ITEMS:
            if name in existing:
                continue
            db.add(
                MenuItem(
                    category=category,
                    name=name,
                    current_price=current_price,
                    legacy_price=legacy_price,
                    is_active=is_active,
                    dietary_markers=DIETARY_MARKERS.get(name),
                )
            )
            added += 1
        db.commit()
        return added
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":
    added = seed()
    print(f"Seeded {added} catalogue items.")
