"""phase19 allow booking_events DELETE only inside a flagged purge

booking_events is append-only (phase1 trigger), which is exactly right for
every normal operation -- an event must never be rewritten or quietly
removed. But hard-deleting a test/erroneous booking has to take its events
with it, or the child rows outlive the parent and violate the FK.

Rather than weaken the guarantee, the trigger now permits a DELETE only
when the session has explicitly set `app.allow_booking_purge = 'on'` --
which only app.services.booking.delete_booking_and_dependents does, via
SET LOCAL (transaction-scoped, auto-reverts). UPDATE stays forbidden
unconditionally: there is never a legitimate reason to rewrite history.

Revision ID: c4f1a9d2e6b8
Revises: a5579e36069c
"""

from alembic import op

revision = "c4f1a9d2e6b8"
down_revision = "a5579e36069c"
branch_labels = None
depends_on = None


_PURGE_AWARE = """
CREATE OR REPLACE FUNCTION prevent_booking_events_mutation() RETURNS trigger AS $$
BEGIN
    -- A DELETE is allowed only inside an explicit, flagged purge; every
    -- other DELETE, and every UPDATE, is refused.
    IF TG_OP = 'DELETE' AND current_setting('app.allow_booking_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'booking_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql
"""

_STRICT = """
CREATE OR REPLACE FUNCTION prevent_booking_events_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'booking_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_PURGE_AWARE)


def downgrade() -> None:
    op.execute(_STRICT)
