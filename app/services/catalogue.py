"""Menu item lookup and price resolution. Pricing math is delegated to the
figures already sitting on MenuItem rows -- this module doesn't invent or
compute a price, it only decides *which* stored price applies to a given
booking (current vs legacy).
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, MenuItem
from app.models.menu_item import MenuItemCategory
from app.services.policy import PIZZA_LEGACY_PRICING_CUTOVER_DATE


def get_active_items(db: Session, category: MenuItemCategory) -> list[MenuItem]:
    return list(
        db.scalars(
            select(MenuItem).where(MenuItem.category == category, MenuItem.is_active.is_(True)).order_by(MenuItem.name)
        ).all()
    )


def get_by_id(db: Session, menu_item_id) -> MenuItem | None:
    item = db.get(MenuItem, menu_item_id)
    if item is None or not item.is_active:
        return None
    return item


def resolve_pizza_price(menu_item: MenuItem, booking: Booking) -> Decimal | None:
    """None means: this booking is legacy-priced, but no legacy price was
    ever defined for this item (e.g. Vegetarian Pizza, added new in Master
    Policy v1.3 -- it has no pre-cutover price because it didn't exist
    yet). A real, expected edge case, not a bug -- the caller surfaces this
    as [REVIEW], never guesses a figure."""
    is_legacy_booking = booking.created_at.date() < PIZZA_LEGACY_PRICING_CUTOVER_DATE
    if not is_legacy_booking:
        return menu_item.current_price
    return menu_item.legacy_price


def resolve_price(menu_item: MenuItem, booking: Booking) -> Decimal | None:
    """Dispatches by category. Platters and cake have no legacy variant at
    all (Master Policy v1.3's "Legacy pricing" section covers pizzas
    only) -- they always price at current_price."""
    if menu_item.category == MenuItemCategory.pizza:
        return resolve_pizza_price(menu_item, booking)
    return menu_item.current_price
