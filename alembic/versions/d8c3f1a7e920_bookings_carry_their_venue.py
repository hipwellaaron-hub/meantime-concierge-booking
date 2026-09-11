"""bookings carry their venue explicitly

Aaron's rule: "Bookings carry their venue explicitly, never inferred from the
reference prefix or the space." and "A booking can never move between venues."

Until now a booking's venue was a derived fact, reachable only by joining
through its space, so it moved whenever the space moved. This makes it a
column, and then makes BOTH halves of the rule properties of the database
rather than of the service layer:

  * A COMPOSITE FOREIGN KEY (space_id, venue_id) -> spaces (id, venue_id)
    makes it structurally impossible for a booking's venue to disagree with
    its space's venue. Not a check that has to be remembered -- a row that
    disagrees cannot be written at all. This needs UNIQUE (id, venue_id) on
    spaces, which is redundant with the primary key but is what a composite
    FK has to point at.

  * A TRIGGER refuses any UPDATE that changes venue_id. The service layer
    already refuses cross-venue moves (booking.assign_space_and_time,
    add_linked_space, create_hold), but that is a property of those three
    functions; this is a property of the table. Same shape as the existing
    prevent_booking_events_mutation trigger, and the same reasoning as the
    food-order rule Aaron settled on 2026-09-11: a rule worth having is
    worth making a property of the server rather than of the caller.

The backfill is per-row from each booking's own space, the same
nullable -> UPDATE -> NOT NULL dance as agreed_min_adults in 1b7abf008413.

Revision ID: d8c3f1a7e920
Revises: a3f6e1c7d094
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd8c3f1a7e920'
down_revision: Union[str, Sequence[str], None] = 'a3f6e1c7d094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A composite FK must reference a uniquely-constrained column pair.
    # (id) is already unique as the primary key, so this adds nothing
    # semantically -- it exists solely so (space_id, venue_id) has something
    # to point at.
    op.create_unique_constraint('uq_spaces_id_venue', 'spaces', ['id', 'venue_id'])

    # No server_default: the right value depends on which space each existing
    # booking is in, so it is backfilled per row below and then flipped to
    # NOT NULL.
    op.add_column('bookings', sa.Column('venue_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE bookings SET venue_id = spaces.venue_id
        FROM spaces WHERE bookings.space_id = spaces.id
        """
    )
    op.alter_column('bookings', 'venue_id', nullable=False)

    op.create_foreign_key(
        'fk_bookings_space_venue',
        'bookings', 'spaces',
        ['space_id', 'venue_id'], ['id', 'venue_id'],
    )
    op.create_index('ix_bookings_venue_id', 'bookings', ['venue_id'])

    # ROLLBACK SAFETY. venue_id is NOT NULL with no default, and a default
    # cannot be a constant because the right value depends on the space. So
    # if this ships and the previous build is then redeployed, the OLD code
    # inserts a booking without venue_id and every insert fails -- including
    # the public enquiry form. "Roll back by redeploying the last build",
    # which is how every migration before this one was safe, would take the
    # enquiry path down.
    #
    # This trigger fills venue_id from the space when an INSERT omits it, so
    # the old code keeps working against the new schema. It does not weaken
    # the explicit-venue rule: the app always sets it (see
    # booking.create_booking), a test pins that, and the composite FK above
    # still refuses anything that disagrees with the space. It is a net for
    # the rollback window, not a second way of doing it.
    op.execute(
        """
        CREATE FUNCTION fill_booking_venue_from_space() RETURNS trigger AS $$
        BEGIN
            IF NEW.venue_id IS NULL THEN
                SELECT venue_id INTO NEW.venue_id FROM spaces WHERE id = NEW.space_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bookings_venue_defaults_from_space
        BEFORE INSERT ON bookings
        FOR EACH ROW EXECUTE FUNCTION fill_booking_venue_from_space()
        """
    )

    # A booking can NEVER move between venues. The three service paths that
    # assign a space already refuse it; this makes it impossible rather than
    # merely refused, including for a hand-written UPDATE.
    op.execute(
        """
        CREATE FUNCTION prevent_booking_venue_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.venue_id IS DISTINCT FROM OLD.venue_id THEN
                RAISE EXCEPTION
                    'a booking cannot move between venues (booking %, venue % -> %)',
                    OLD.id, OLD.venue_id, NEW.venue_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bookings_venue_is_immutable
        BEFORE UPDATE ON bookings
        FOR EACH ROW EXECUTE FUNCTION prevent_booking_venue_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bookings_venue_is_immutable ON bookings")
    op.execute("DROP FUNCTION IF EXISTS prevent_booking_venue_change()")
    op.execute("DROP TRIGGER IF EXISTS trg_bookings_venue_defaults_from_space ON bookings")
    op.execute("DROP FUNCTION IF EXISTS fill_booking_venue_from_space()")
    op.drop_index('ix_bookings_venue_id', table_name='bookings')
    op.drop_constraint('fk_bookings_space_venue', 'bookings', type_='foreignkey')
    op.drop_column('bookings', 'venue_id')
    op.drop_constraint('uq_spaces_id_venue', 'spaces', type_='unique')
