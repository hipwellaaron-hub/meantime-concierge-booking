"""phase17 dietary markers on menu items, plus the Custom Vegan Platter

Revision ID: 2d4e897f8ad1
Revises: 5c43095053c2
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2d4e897f8ad1'
down_revision: Union[str, Sequence[str], None] = '5c43095053c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # dietary_markers: list of marker codes (V, VG, DF, DFA) as published
    # on the live website menu. NULL means "not yet confirmed against the
    # website" -- distinct from [] which means "confirmed to carry no
    # marker". contains_peanuts exists per the venue's allergen audit so a
    # peanut marker CAN be set per item; the function menu is peanut-clear
    # today, so nothing sets it yet.
    op.add_column('menu_items', sa.Column('dietary_markers', postgresql.JSONB(), nullable=True))
    op.add_column(
        'menu_items', sa.Column('contains_peanuts', sa.Boolean(), nullable=False, server_default=sa.false())
    )

    # Markers sourced from the live website menu screenshots (28 Aug 2026)
    # -- ONLY items confirmed there get a value; everything else stays
    # NULL rather than guessed. Names differ slightly between the website
    # and this catalogue; mapping per Aaron's brief.
    op.execute("""UPDATE menu_items SET dietary_markers = '["V", "DF"]' WHERE name = 'Shoestring Fries'""")
    op.execute("""UPDATE menu_items SET dietary_markers = '["V"]' WHERE name = 'Crispy Popcorn Halloumi'""")
    op.execute("""UPDATE menu_items SET dietary_markers = '["DFA"]' WHERE name = 'Salt & Pepper Squid'""")
    op.execute("""UPDATE menu_items SET dietary_markers = '["DF"]' WHERE name = 'Pork Belly Bites'""")
    op.execute("""UPDATE menu_items SET dietary_markers = '["V"]' WHERE name = 'Margherita Pizza'""")
    op.execute("""UPDATE menu_items SET dietary_markers = '[]' WHERE name IN ('Prosciutto & Pear', 'Spiced Lamb', 'Calabrese')""")

    # New catalogue item, missing from the wizard entirely until now:
    # Garlic Bread, Sticky Chilli Tofu, Corn Ribs, Wedges.
    op.execute(
        """
        INSERT INTO menu_items (id, category, name, current_price, legacy_price, is_active, dietary_markers, contains_peanuts, created_at, updated_at)
        SELECT gen_random_uuid(), 'platter', 'Custom Vegan Platter', 140.00, NULL, TRUE, '["VG"]', FALSE, now(), now()
        WHERE NOT EXISTS (SELECT 1 FROM menu_items WHERE name = 'Custom Vegan Platter')
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM menu_items WHERE name = 'Custom Vegan Platter'")
    op.drop_column('menu_items', 'contains_peanuts')
    op.drop_column('menu_items', 'dietary_markers')
