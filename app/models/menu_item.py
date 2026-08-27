import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MenuItemCategory(str, enum.Enum):
    platter = "platter"
    pizza = "pizza"
    cake = "cake"
    # Added in phase16 so the Event Order's food grouping (Platters /
    # Pizzas / Sides / Desserts) is purely category-driven, never a
    # name-matching hack. Cakes render under Desserts too; they stay their
    # own category because the wizard's cake picker reads it.
    side = "side"
    dessert = "dessert"


menu_item_category_enum = SAEnum(MenuItemCategory, name="menu_item_category", native_enum=True)


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[MenuItemCategory] = mapped_column(menu_item_category_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Only pizzas ever populate this -- Master Policy v1.3's "Legacy
    # pricing" section appears only under pizzas; platters and cake have
    # no legacy variant at all. NULL for a pizza specifically means "no
    # legacy price was ever defined" (e.g. Vegetarian Pizza, new in v1.3)
    # -- see app/services/catalogue.py, which never guesses a price here.
    legacy_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("category", "name", name="uq_menu_item_category_name"),)
