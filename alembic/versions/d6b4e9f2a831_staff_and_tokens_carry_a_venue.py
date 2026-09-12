"""staff and their floor devices carry a venue

Step 8. Until now StaffUser and StaffAppToken had no venue at all, so the
admin staff page listed every venue's people and every live device token
under one venue's URL and one venue's band -- the mislabelled-page failure
this project hit three times in two days.

AARON'S RULING, 2026-09-12:
  * Floor staff belong to ONE venue. They never see a venue picker and
    cannot choose.
  * ADMINS stay NULL, meaning "works every venue", and get a picker at floor
    sign-in. Aaron is the only admin and works both buildings.

So `staff_users.venue_id` is NULLABLE and NULL is MEANINGFUL -- it is not
"unset", it is "every venue". That is the one place in this codebase where a
NULL venue means something rather than nothing, and it is why this column
does not get the usual "NULL invents nothing" treatment.

`staff_app_tokens.venue_id` is different: a DEVICE is in one building. It is
backfilled to Hamilton for every existing row INCLUDING REVOKED ONES -- a
revoked row keeps its last_used_at as a record of when that device was last
seen, and a record with a hole in it is a worse record.

NULLABLE RATHER THAN NOT NULL, for rollback safety. A NOT NULL column with
no default breaks the previous build's inserts: `issue_app_token` at
c5f8a1d3e720 and earlier writes no venue_id, so a rolled-back build could
not issue a floor token at all -- every phone that signed out would be
locked out. Nullable means the old build keeps working; the NEW build
refuses a token that has no venue and asks that device to sign in again,
which is the honest answer and affects only tokens minted during a rollback
window.

Revision ID: d6b4e9f2a831
Revises: c5f8a1d3e720
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'd6b4e9f2a831'
down_revision: Union[str, Sequence[str], None] = 'c5f8a1d3e720'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hamilton_id(conn):
    """The venue every existing floor account and device belongs to.

    Checked BEFORE either UPDATE, because a subquery that finds nothing
    returns NULL rather than failing -- and `SET venue_id = NULL` matches
    every row and succeeds. That is not a migration that did nothing; it is
    a migration that quietly emptied the column it was adding, locking out
    every phone and leaving every floor account venueless.
    """
    venue_id = conn.execute(
        sa.text("SELECT id FROM venues WHERE slug = 'hamilton'")
    ).scalar()
    if venue_id is None:
        raise RuntimeError(
            "no venue with slug 'hamilton' -- refusing to backfill staff_users.venue_id "
            "and staff_app_tokens.venue_id, because the subquery would resolve to NULL "
            "and null EVERY row rather than fail. Seed the venue first (python -m app.seed)."
        )
    return venue_id


def upgrade() -> None:
    conn = op.get_bind()
    hamilton_id = _hamilton_id(conn)

    # --- the person ---------------------------------------------------------
    op.add_column('staff_users', sa.Column('venue_id', UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_staff_users_venue', 'staff_users', 'venues', ['venue_id'], ['id']
    )
    # Floor staff get Hamilton; ADMINS ARE LEFT NULL on purpose -- that is
    # "every venue", which is what an admin is.
    conn.execute(
        sa.text("UPDATE staff_users SET venue_id = :venue_id WHERE role = 'floor'"),
        {"venue_id": hamilton_id},
    )

    # --- the device ---------------------------------------------------------
    op.add_column('staff_app_tokens', sa.Column('venue_id', UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_staff_app_tokens_venue', 'staff_app_tokens', 'venues', ['venue_id'], ['id']
    )
    # EVERY row, revoked included. Aaron, 2026-09-12: "every existing floor
    # device is Hamilton" -- confirmed rather than assumed, so this states it
    # as a fact rather than leaving it to a default.
    conn.execute(
        sa.text("UPDATE staff_app_tokens SET venue_id = :venue_id"),
        {"venue_id": hamilton_id},
    )
    op.create_index(
        'ix_staff_app_tokens_venue', 'staff_app_tokens', ['venue_id']
    )


def downgrade() -> None:
    op.drop_index('ix_staff_app_tokens_venue', table_name='staff_app_tokens')
    op.drop_constraint('fk_staff_app_tokens_venue', 'staff_app_tokens', type_='foreignkey')
    op.drop_column('staff_app_tokens', 'venue_id')
    op.drop_constraint('fk_staff_users_venue', 'staff_users', type_='foreignkey')
    op.drop_column('staff_users', 'venue_id')
