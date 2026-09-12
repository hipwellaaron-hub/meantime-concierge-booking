"""venue identity as data, not module constants

The Entrance is a DIFFERENT COMPANY -- Nice Try Events Pty Ltd, its own ABN,
its own bank account, its own Stripe account (Aaron, 2026-09-11). Until now
every one of those facts was a module constant in app/services/policy.py,
bound once at import as a Jinja global, and stamped on every client-facing
invoice, agreement and Event Order. There was nowhere to put a second set.

This gives the venue record its own identity. Nothing reads these columns
yet -- the renders move in the next commit, behind the tests written in
667fc94 -- so this migration changes no behaviour at all.

Nullable on purpose, all of them. A venue with no address is a venue nobody
has finished setting up, and the honest answer for a missing value is to
refuse to print rather than to invent one; a NOT NULL column would instead
force a placeholder into the table, which is the shape that puts "TBC" on a
contract. Hamilton is backfilled from the constants it already uses, so it
starts complete.

NO BANKING SECRETS HERE. The Stripe key stays in an environment variable;
what lives on the row is the NAME of that variable plus the account id it is
expected to belong to, so a resolved key can be checked against the venue it
was resolved for. Aaron supplies The Entrance's own values; they are not
written into this file (same rule as the Sebel bedding rate).

Revision ID: e1b6a44c7f83
Revises: d8c3f1a7e920
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e1b6a44c7f83'
down_revision: Union[str, Sequence[str], None] = 'd8c3f1a7e920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ('trading_name', sa.String(255)),
    ('legal_name', sa.String(255)),
    ('abn', sa.String(32)),
    ('address', sa.String(255)),
    ('phone', sa.String(32)),
    ('contact_name', sa.String(120)),
    ('contact_email', sa.String(320)),
    ('bank_account_name', sa.String(255)),
    ('bank_bsb', sa.String(16)),
    ('bank_account_number', sa.String(32)),
    ('licence_number', sa.String(64)),
    ('licensed_manager', sa.String(255)),
    ('reference_prefix', sa.String(5)),
    # The NAME of the environment variable holding this venue's Stripe
    # secret key -- never the key. And the account id that key must belong
    # to, so a resolved credential can be checked against the venue it was
    # resolved for rather than trusted.
    ('stripe_secret_key_env', sa.String(64)),
    ('stripe_account_id', sa.String(64)),
]


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column('venues', sa.Column(name, type_, nullable=True))

    # Hamilton starts complete, from the constants it already uses. Written
    # as one statement keyed on the slug so it is a no-op on any database
    # that does not have Hamilton.
    op.execute(
        """
        UPDATE venues SET
            trading_name        = 'Meantime Hamilton',
            legal_name          = 'Meantime Pty Ltd',
            abn                 = '36 654 270 532',
            address             = '104 Beaumont St, Hamilton NSW 2303',
            phone               = '(02) 40410697',
            contact_name        = 'Aaron',
            contact_email       = 'meantimehamilton@gmail.com',
            bank_account_name   = 'Meantime Pty Ltd',
            bank_bsb            = '063-519',
            bank_account_number = '10315591',
            reference_prefix    = 'HAM',
            stripe_secret_key_env = 'STRIPE_SECRET_KEY'
        WHERE slug = 'hamilton'
        """
    )

    # A reference prefix has to be unique or two venues' references collide,
    # and reference_code is only String(20) -- date plus suffix leaves five
    # characters, which is why the column is String(5).
    op.create_unique_constraint('uq_venues_reference_prefix', 'venues', ['reference_prefix'])


def downgrade() -> None:
    op.drop_constraint('uq_venues_reference_prefix', 'venues', type_='unique')
    for name, _type in reversed(COLUMNS):
        op.drop_column('venues', name)
