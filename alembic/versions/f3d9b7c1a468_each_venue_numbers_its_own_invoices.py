"""each venue numbers its own invoices

Two legal entities -- Meantime Pty Ltd and Nice Try Events Pty Ltd -- must
each keep their own tax-invoice register. Today every invoice draws from one
Postgres sequence with a UNIQUE across the whole table, so the two
companies' invoices would interleave and each register would carry gaps it
could not explain.

WHAT A CLIENT READS. `HAM-1004`, `ENT-1001`: the venue's reference_prefix
and that venue's own number. The same shape as the booking reference
`HAM-20261114-AB12C` printed two rows above it on the same page, so an
invoice number becomes the same kind of object somebody already reads back
over the phone. Hamilton's existing numbers keep their VALUES and gain the
prefix -- 1004 stays 1004, and is displayed HAM-1004.

THE REFERENCE IS STORED, NOT DERIVED, for the reason bookings.reference_code
is stored: a client holds it, so it must not change when a row is edited.
It carries the global UNIQUE that invoice_number used to carry, which is
what makes a quoted number resolve to exactly one invoice.

THIS MIGRATION REFUSES IF A SECOND VENUE ALREADY HOLDS INVOICES (Aaron,
2026-09-13). That case is not merely untested, it is unrecoverable: the
seed would have to decide what each register's position means, and an
earlier draft of this design got it wrong in a way that put both entities'
series in permanent lockstep on the same integers. Refusing costs nothing,
because the whole point of doing this BEFORE The Entrance's row exists is
that the case cannot arise.

Revision ID: f3d9b7c1a468
Revises: e8a1c6f4b209
Create Date: 2026-09-13 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3d9b7c1a468"
down_revision: Union[str, Sequence[str], None] = "b7e4a91c3f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Where a register starts when nobody has said otherwise. The house
# convention, inherited from invoice_number_seq's own START WITH 1001 --
# not a venue-specific fact, so it is a default rather than a refusal.
# The venue-specific fact is reference_prefix, and THAT refuses.
FIRST_NUMBER = 1001


def _preflight(conn) -> None:
    """Everything that must be true before a single statement runs.

    Each of these aborts the whole migration, which is one transaction, so
    a refusal leaves the database exactly as it was.
    """
    venues_with_invoices = conn.execute(
        sa.text(
            "SELECT DISTINCT b.venue_id FROM invoices i JOIN bookings b ON b.id = i.booking_id"
        )
    ).scalars().all()

    if len(venues_with_invoices) > 1:
        raise RuntimeError(
            f"{len(venues_with_invoices)} venues already hold invoices. This migration splits ONE "
            "shared register into per-venue registers and cannot decide where each of several "
            "existing series should resume -- the answer is a business decision, not a default. "
            "Refusing rather than guessing."
        )

    # Every venue that holds invoices, and every venue at all, needs a
    # prefix before it can build a reference. Checked for the ones that
    # hold invoices now, because those are the rows being backfilled; a
    # venue with no prefix simply cannot issue an invoice later, and the
    # trigger says so at that point.
    missing = conn.execute(
        sa.text(
            "SELECT v.slug FROM venues v WHERE v.id = ANY(:ids) "
            "AND (v.reference_prefix IS NULL OR btrim(v.reference_prefix) = '')"
        ),
        {"ids": list(venues_with_invoices)},
    ).scalars().all()
    if missing:
        raise RuntimeError(
            f"venue(s) {', '.join(missing)} hold invoices but have no reference_prefix, so no "
            "invoice reference can be built for them. Set reference_prefix first."
        )

    clashes = conn.execute(
        sa.text(
            "SELECT i.invoice_number, count(*) FROM invoices i "
            "GROUP BY i.invoice_number HAVING count(*) > 1"
        )
    ).all()
    if clashes:
        raise RuntimeError(f"duplicate invoice_number values already exist: {clashes}")


def upgrade() -> None:
    conn = op.get_bind()
    # Fail fast rather than queue behind a live transaction and then take
    # ACCESS EXCLUSIVE on invoices for however long that takes.
    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    _preflight(conn)

    # --- the register ------------------------------------------------------
    op.create_table(
        "venue_invoice_counters",
        sa.Column("venue_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("venues.id"), primary_key=True),
        # The number the NEXT invoice for this venue will take.
        sa.Column("next_number", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("next_number > 0", name="ck_venue_invoice_counters_positive"),
    )

    # --- the columns -------------------------------------------------------
    op.add_column("invoices", sa.Column("venue_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("invoices", sa.Column("invoice_reference", sa.String(32), nullable=True))
    op.create_foreign_key("fk_invoices_venue", "invoices", "venues", ["venue_id"], ["id"])

    conn.execute(sa.text(
        "UPDATE invoices i SET venue_id = b.venue_id FROM bookings b "
        "WHERE b.id = i.booking_id AND i.venue_id IS NULL"
    ))
    # The reference for every row already in the table: THIS venue's
    # prefix and the number the invoice already has. No number changes.
    filled = conn.execute(sa.text(
        "UPDATE invoices i SET invoice_reference = v.reference_prefix || '-' || i.invoice_number "
        "FROM venues v WHERE v.id = i.venue_id AND i.invoice_reference IS NULL"
    ))
    print(f"invoice_reference: {filled.rowcount} existing invoice(s) given their venue's prefix")

    op.alter_column("invoices", "venue_id", nullable=False)
    op.alter_column("invoices", "invoice_reference", nullable=False)

    # --- uniqueness moves ---------------------------------------------------
    # Per venue for the integer: ENT-1001 and HAM-1001 are different
    # invoices. Globally for the REFERENCE, which is what a client quotes
    # and what therefore has to resolve to exactly one row.
    op.drop_constraint("uq_invoices_invoice_number", "invoices", type_="unique")
    op.create_unique_constraint(
        "uq_invoices_venue_number", "invoices", ["venue_id", "invoice_number"]
    )
    op.create_unique_constraint("uq_invoices_reference", "invoices", ["invoice_reference"])

    # --- seed each register -------------------------------------------------
    # From THIS venue's own highest number, never from the retired
    # sequence's position. The sequence may sit well above the highest
    # number actually issued (a rolled-back draft burns one), and it is a
    # single shared position that means nothing to a second venue. A venue
    # holding no invoices starts at FIRST_NUMBER.
    conn.execute(
        sa.text(
            "INSERT INTO venue_invoice_counters (venue_id, next_number) "
            "SELECT v.id, COALESCE(MAX(i.invoice_number) + 1, :first) "
            "FROM venues v LEFT JOIN invoices i ON i.venue_id = v.id "
            "GROUP BY v.id ON CONFLICT (venue_id) DO NOTHING"
        ),
        {"first": FIRST_NUMBER},
    )
    for venue_id, slug, nxt in conn.execute(sa.text(
        "SELECT c.venue_id, v.slug, c.next_number FROM venue_invoice_counters c "
        "JOIN venues v ON v.id = c.venue_id ORDER BY v.slug"
    )).all():
        print(f"invoice register opened: {slug} resumes at {nxt}")

    # --- the allocator ------------------------------------------------------
    # The column default goes: a default cannot see which venue the row
    # belongs to, and leaving it in place would evaluate nextval on every
    # insert and keep burning numbers out of the retired sequence.
    op.alter_column("invoices", "invoice_number", server_default=None)

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION assign_invoice_number() RETURNS trigger AS $$
        DECLARE
            v_prefix text;
            v_number integer;
        BEGIN
            -- venue_id is taken FROM THE BOOKING, always, whatever the
            -- caller supplied. The column then cannot disagree with the
            -- booking it belongs to; the same reasoning as
            -- fill_booking_venue_from_space.
            SELECT b.venue_id INTO NEW.venue_id FROM bookings b WHERE b.id = NEW.booking_id;
            IF NEW.venue_id IS NULL THEN
                RAISE EXCEPTION 'invoice % has no booking to take a venue from', NEW.id;
            END IF;

            SELECT btrim(COALESCE(v.reference_prefix, '')) INTO v_prefix
            FROM venues v WHERE v.id = NEW.venue_id;
            IF v_prefix = '' THEN
                RAISE EXCEPTION
                    'venue % has no reference_prefix, so no invoice reference can be built for it',
                    NEW.venue_id;
            END IF;

            IF NEW.invoice_number IS NULL THEN
                -- One statement, so it is a row lock: two inserts for the
                -- same venue at the same instant serialise here and take
                -- consecutive numbers. A rolled-back transaction gives its
                -- number back, which a sequence cannot do.
                UPDATE venue_invoice_counters
                   SET next_number = next_number + 1
                 WHERE venue_id = NEW.venue_id
                RETURNING next_number - 1 INTO v_number;

                IF v_number IS NULL THEN
                    -- No register yet. Opening one at the house start is
                    -- not a guess: 1001 is the convention the original
                    -- sequence used, and it is not a venue-specific fact.
                    -- The venue-specific fact is the prefix, and that
                    -- refused above.
                    INSERT INTO venue_invoice_counters (venue_id, next_number)
                    VALUES (NEW.venue_id, {FIRST_NUMBER} + 1)
                    ON CONFLICT (venue_id) DO UPDATE SET next_number = venue_invoice_counters.next_number + 1
                    RETURNING next_number - 1 INTO v_number;
                END IF;

                NEW.invoice_number := v_number;
            END IF;

            -- Only BUILD a reference when the row does not already carry
            -- one. A pg_restore re-fires this trigger, and overwriting
            -- here would rebuild every historical reference from the
            -- venue's CURRENT prefix -- silently rewriting what clients
            -- hold, which is the exact thing storing the reference rather
            -- than deriving it exists to prevent.
            --
            -- Symmetrical with invoice_number above: a row that arrives
            -- carrying its own identity keeps it.
            IF NEW.invoice_reference IS NULL THEN
                NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_assign_number ON invoices")
    op.execute(
        "CREATE TRIGGER trg_invoices_assign_number BEFORE INSERT ON invoices "
        "FOR EACH ROW EXECUTE FUNCTION assign_invoice_number()"
    )

    # --- and it never changes again ----------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION freeze_invoice_identity() RETURNS trigger AS $$
        BEGIN
            IF NEW.invoice_number IS DISTINCT FROM OLD.invoice_number THEN
                RAISE EXCEPTION 'an invoice number cannot be rewritten (invoice %, % -> %)',
                    OLD.id, OLD.invoice_number, NEW.invoice_number;
            END IF;
            IF NEW.invoice_reference IS DISTINCT FROM OLD.invoice_reference THEN
                RAISE EXCEPTION 'an invoice reference cannot be rewritten (invoice %, % -> %)',
                    OLD.id, OLD.invoice_reference, NEW.invoice_reference;
            END IF;
            IF NEW.venue_id IS DISTINCT FROM OLD.venue_id THEN
                RAISE EXCEPTION 'an invoice cannot move between venues (invoice %)', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_freeze_identity ON invoices")
    op.execute(
        "CREATE TRIGGER trg_invoices_freeze_identity BEFORE UPDATE ON invoices "
        "FOR EACH ROW EXECUTE FUNCTION freeze_invoice_identity()"
    )

    # invoice_number_seq is left exactly where it is, owned by the column
    # it no longer fills. Dropping it would throw away the one record of
    # where Hamilton's shared series got to, and it costs nothing to keep.


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_freeze_identity ON invoices")
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_assign_number ON invoices")
    op.execute("DROP FUNCTION IF EXISTS freeze_invoice_identity()")
    op.execute("DROP FUNCTION IF EXISTS assign_invoice_number()")
    op.drop_constraint("uq_invoices_reference", "invoices", type_="unique")
    op.drop_constraint("uq_invoices_venue_number", "invoices", type_="unique")
    # Only restorable while no two venues share a number. Any invoice
    # issued to a second venue under the new scheme keeps its number, so
    # this fails loudly rather than renumbering somebody's invoice.
    op.create_unique_constraint("uq_invoices_invoice_number", "invoices", ["invoice_number"])
    op.alter_column(
        "invoices", "invoice_number",
        server_default=sa.text("nextval('invoice_number_seq')"),
    )
    # AND MOVE THE SEQUENCE PAST EVERYTHING THE COUNTER ISSUED.
    #
    # Nothing has called nextval since the upgrade ran -- the column
    # default was dropped, so the sequence stood still while
    # venue_invoice_counters climbed. Restoring the default without this
    # hands out numbers that are already taken: the insert path then raises
    # UniqueViolation once per invoice issued since the cutover, on the
    # public deposit-invoice route, while the downgrade itself exits 0 and
    # looks like a clean rollback.
    #
    # GLOBAL max, because the constraint restored above is global.
    op.execute(
        "SELECT setval('invoice_number_seq', "
        "GREATEST((SELECT COALESCE(MAX(invoice_number), 1000) FROM invoices), "
        "(SELECT last_value FROM invoice_number_seq)), true)"
    )
    op.drop_constraint("fk_invoices_venue", "invoices", type_="foreignkey")
    op.drop_column("invoices", "invoice_reference")
    op.drop_column("invoices", "venue_id")
    op.drop_table("venue_invoice_counters")
