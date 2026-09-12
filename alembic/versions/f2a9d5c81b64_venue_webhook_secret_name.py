"""a venue names its own Stripe webhook signing secret

stripe.Webhook.construct_event verifies against exactly ONE signing secret,
so one endpoint cannot verify two companies' accounts. Each venue gets its
own webhook path, and the path has to find that venue's secret.

Stored the same way as the API key: the NAME of the environment variable,
never the secret. Hamilton's is backfilled to the variable it already uses,
so the legacy path and the new per-venue path resolve to the same secret and
nothing changes for it.

A separate migration rather than an edit to e1b6a44c7f83, which has already
been applied locally -- editing an applied migration leaves two databases
disagreeing about what that revision did.

Revision ID: f2a9d5c81b64
Revises: e1b6a44c7f83
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2a9d5c81b64'
down_revision: Union[str, Sequence[str], None] = 'e1b6a44c7f83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('stripe_webhook_secret_env', sa.String(64), nullable=True))
    op.execute(
        "UPDATE venues SET stripe_webhook_secret_env = 'STRIPE_WEBHOOK_SECRET' WHERE slug = 'hamilton'"
    )


def downgrade() -> None:
    op.drop_column('venues', 'stripe_webhook_secret_env')
