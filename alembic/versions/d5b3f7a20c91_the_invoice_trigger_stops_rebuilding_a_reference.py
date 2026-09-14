"""the invoice trigger stops rebuilding a reference on restore

A FIX THAT SHIPPED AND NEVER RAN.

f3d9b7c1a468 creates assign_invoice_number(), and its first version ended
with an unconditional

    NEW.invoice_reference := v_prefix || '-' || NEW.invoice_number;

An adversarial review found that pg_restore re-fires BEFORE INSERT
triggers, so restoring a dump taken before a venue's reference_prefix
changed would silently rewrite every reference a client holds -- the exact
thing storing the reference rather than deriving it exists to prevent. The
fix, an IF NEW.invoice_reference IS NULL guard, was written into
f3d9b7c1a468's own upgrade() and deployed.

It could not take. f3d9b7c1a468 had ALREADY RUN on production forty-five
seconds into the deploy before it, so alembic read the database as up to
date and skipped the file entirely. Proven from the deployment record
rather than reasoned about:

    04:15:54  Running upgrade b7e4a91c3f20 -> f3d9b7c1a468   (pre-fix body)
    04:25:47  Context impl PostgresqlImpl / transactional DDL, and nothing
              else -- no upgrade line at all

and confirmed against a real database: concierge_dev's pg_proc.prosrc for
assign_invoice_number carries the unguarded assignment today.

THIS IS THE GENERAL LESSON, not a one-off. Editing an applied migration
changes what a FRESH database gets and nothing else. Every database that
already ran it keeps the old behaviour forever, silently, and the code
review that approved the fix is no evidence it is in effect anywhere. A
correction to something already applied has to be its own revision.

CREATE OR REPLACE, so this is a no-op on a database built after the fix --
the body below is byte-identical to the one f3d9b7c1a468 now installs, and
a test asserts that rather than trusting it. The trigger itself is not
touched: it already points at this function by name.

NO BACKFILL, and that is deliberate. Nothing is wrong with the references
sitting in the table -- the hazard is what a future restore would do to
them. Rewriting rows here would be doing the damage the guard prevents.

Revision ID: d5b3f7a20c91
Revises: c2f8d61a94b7
Create Date: 2026-09-14 06:10:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d5b3f7a20c91"
down_revision: Union[str, Sequence[str], None] = "c2f8d61a94b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept here verbatim rather than imported from f3d9b7c1a468, because a
# migration is a frozen snapshot: importing would let a later edit to that
# file change retroactively what THIS revision did on the day it ran.
# tests/test_the_invoice_trigger_keeps_a_restored_reference.py compares the
# two strings, so the copy cannot drift instead.
GUARDED_ASSIGN_INVOICE_NUMBER = """
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
                    VALUES (NEW.venue_id, 1001 + 1)
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


def upgrade() -> None:
    op.execute(GUARDED_ASSIGN_INVOICE_NUMBER)


def downgrade() -> None:
    # Deliberately NOT restoring the unguarded body. A downgrade exists to
    # undo a schema change; putting back a function that silently rewrites
    # what clients hold would be restoring a defect, not a shape. The
    # revision before this one drops the function outright anyway.
    pass
