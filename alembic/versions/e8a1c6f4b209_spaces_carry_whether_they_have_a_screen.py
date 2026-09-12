"""spaces carry whether they have a screen

The Event Order's AV/Screen section was gated on

    if booking.space.name != "The Loft"

-- a Hamilton ROOM NAME as a string literal, standing in for a property of
the room. Two ways that is wrong once there is a second venue: a room at
The Entrance with a screen can never have the section, and any room
anywhere that happens to be called "The Loft" inherits it, screen or not.

The capability becomes a column, which is what it always was. The existing
booleans on this table (wheelchair_accessible, has_per_head_shortfall_fee)
are the same shape, for the same reason.

THE BACKFILL IS BY NAME, deliberately, and that is not the same mistake it
replaces. This is a one-time data statement against a known state -- one
venue, one room with a screen in it -- not a rule the code will keep
consulting. The rule is the column. A migration that ran the same query at
runtime would be the bug.

Revision ID: e8a1c6f4b209
Revises: d6b4e9f2a831
Create Date: 2026-09-13 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1c6f4b209"
down_revision: Union[str, Sequence[str], None] = "d6b4e9f2a831"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows get FALSE rather than blocking the
    # NOT NULL; the model carries default=False for new rows created
    # through the ORM.
    op.add_column(
        "spaces",
        sa.Column("has_screen", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # The screen is physically in Hamilton's Loft. Nothing else has one.
    conn = op.get_bind()
    result = conn.execute(
        sa.text("UPDATE spaces SET has_screen = true WHERE name = 'The Loft'")
    )
    # Said out loud in the deploy log rather than assumed. A backfill that
    # matched nothing would silently drop the AV section off every Event
    # Order for the one room that has a screen, and the section not
    # rendering looks exactly like a booking that did not ask for AV.
    print(f"spaces.has_screen: {result.rowcount} room(s) marked as having a screen")

    # Dropped once the existing rows are set: a column default that stays
    # is a default, and a new room's capability should be stated rather
    # than inherited.
    op.alter_column("spaces", "has_screen", server_default=None)


def downgrade() -> None:
    op.drop_column("spaces", "has_screen")
