"""phase16 BEO overhaul: enum values and catalogue retirement

Revision ID: 37b4408668f8
Revises: 3ad9742be8fc
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '37b4408668f8'
down_revision: Union[str, Sequence[str], None] = '3ad9742be8fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New wizard steps (vendors for everyone, av for Loft bookings only --
    # gating is app logic, the enum just has to know the values) and food
    # categories so grouping stays category-driven rather than name-hacked.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE wizard_step ADD VALUE IF NOT EXISTS 'vendors'")
        op.execute("ALTER TYPE wizard_step ADD VALUE IF NOT EXISTS 'av'")
        op.execute("ALTER TYPE menu_item_category ADD VALUE IF NOT EXISTS 'side'")
        op.execute("ALTER TYPE menu_item_category ADD VALUE IF NOT EXISTS 'dessert'")

    # Recategorize: fries were never really a platter, and the retired
    # Dessert Platter moves to dessert (while inactive) purely so orders
    # that already reference it group under the right heading.
    op.execute("UPDATE menu_items SET category = 'side' WHERE name = 'Shoestring Fries'")
    op.execute("UPDATE menu_items SET category = 'dessert' WHERE name = 'Dessert Platter'")

    # Catalogue retirement (per Aaron, 27 Aug 2026): the layered cakes and
    # the $140 mixed Dessert Platter are discontinued at Hamilton.
    # Deactivated, never deleted -- existing bookings referencing these
    # rows still resolve to their name and quoted price; they simply stop
    # being offered for new selection (get_active_items filters them out).
    op.execute(
        """
        UPDATE menu_items SET is_active = FALSE
        WHERE name IN (
            'Vanilla Cake (2 Layer)', 'Vanilla Cake (3 Layer)', 'Vanilla Cake (4 Layer)',
            'Chocolate Cake (2 Layer)', 'Chocolate Cake (3 Layer)', 'Chocolate Cake (4 Layer)',
            'Dessert Platter'
        )
        """
    )


def downgrade() -> None:
    """Postgres cannot remove enum values; the retirement flags could be
    reversed but the enum change cannot, so this migration is one-way --
    same stance as 54e0e201860e."""
    raise NotImplementedError("phase16 enum additions cannot be downgraded")
