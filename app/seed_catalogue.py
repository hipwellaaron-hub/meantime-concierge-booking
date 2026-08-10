"""Idempotent seed of the function-catering catalogue (platters, pizzas),
sourced verbatim from the Meantime Hamilton Master Policy v1.3 doc
(locked, August 2026), sections 1.4 and 1.5.

Legacy prices are the pre-May-2026 figures the doc gives for pizzas only
-- platters have no legacy variant (see app.services.catalogue). Vegetarian
Pizza has no legacy price: it's a new item introduced in v1.3, so it never
had a pre-cutover price to honour.

No cake rows are seeded here -- the in-house cake catalogue doesn't exist
in machine-readable form yet (see app.services.wizard's extras step).

Run with: python -m app.seed_catalogue
"""

from decimal import Decimal

from app.database import SessionLocal
from app.models import MenuItem
from app.models.menu_item import MenuItemCategory

# (category, name, current_price, legacy_price)
CATALOGUE_ITEMS = [
    (MenuItemCategory.platter, "Pork Belly Bites", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Chicken Tender Skewers", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Salt & Pepper Squid", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Mushroom & Feta Arancini", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Crispy Popcorn Halloumi", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Pork & Fennel Meatballs", Decimal("100.00"), None),
    (MenuItemCategory.platter, "Ricotta & Sun-dried Tomato Stuffed Mushrooms", Decimal("80.00"), None),
    (MenuItemCategory.platter, "Grazing Platter", Decimal("250.00"), None),
    (MenuItemCategory.platter, "Shoestring Fries", Decimal("15.00"), None),
    (MenuItemCategory.pizza, "Margherita Pizza", Decimal("27.00"), Decimal("26.00")),
    (MenuItemCategory.pizza, "Vegetarian Pizza", Decimal("30.00"), None),  # new item in v1.3, no legacy price
    (MenuItemCategory.pizza, "Prosciutto & Pear", Decimal("34.00"), Decimal("30.00")),
    (MenuItemCategory.pizza, "Spiced Lamb", Decimal("34.00"), Decimal("30.00")),
    (MenuItemCategory.pizza, "Calabrese", Decimal("34.00"), Decimal("30.00")),
]


def seed(db=None) -> int:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        existing = {(item.category, item.name) for item in db.query(MenuItem)}
        added = 0
        for category, name, current_price, legacy_price in CATALOGUE_ITEMS:
            if (category, name) in existing:
                continue
            db.add(
                MenuItem(
                    category=category,
                    name=name,
                    current_price=current_price,
                    legacy_price=legacy_price,
                    is_active=True,
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
