"""phase20 tracking: per-platform conversion dispatch timestamps

The thank-you page is eligible to fire a conversion the moment the enquiry
is created (backend success), but "eligible" is NOT "emitted". These
columns record when the BROWSER actually dispatched each platform's event
(confirmed by a beacon after gtag/fbq run), never when the server merely
rendered the page -- so a browser that closes, or a blocked tag, before
dispatch does not permanently suppress a real conversion. Per platform, so
GA4 being blocked while Meta succeeds (or vice versa) leaves the other free
to fire on a later load. Named "dispatched" not "received": the browser
attempted the send; platform receipt is verified separately in GA4
DebugView / Meta Test Events.

Nullable, default NULL -- every existing booking is correctly "never
dispatched" and nothing historical needs backfilling.

Revision ID: d5b1c7e93a42
Revises: c4f1a9d2e6b8
"""

import sqlalchemy as sa
from alembic import op

revision = "d5b1c7e93a42"
down_revision = "c4f1a9d2e6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("ga4_conversion_dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("meta_conversion_dispatched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "meta_conversion_dispatched_at")
    op.drop_column("bookings", "ga4_conversion_dispatched_at")
