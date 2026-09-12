"""a venue names who receives its digest

Aaron, 2026-09-12 (question 4): a column, seeded to his address for both
venues, so the loop has somewhere per-venue to point later.

Today DIGEST_RECIPIENT_EMAIL is a single process-wide environment variable on
the digest service. That is exactly one answer, and the digest is about to
cover two companies. The column is per venue; the sender groups venues BY
recipient, so with both pointing at the same address it stays one email with
a section per venue -- which is what Aaron asked for, because two emails
means one gets skimmed -- and the day The Entrance's digest should go
somewhere else, it splits on its own with no code change.

NULLABLE, and not backfilled with a literal address. There is no address to
put here that is not already in DIGEST_RECIPIENT_EMAIL, and duplicating it
into the database would give two sources for one fact. NULL means "use the
process-wide variable", which is today's behaviour exactly.

Revision ID: b7c2d8e4f316
Revises: a4e7b2f9c105
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c2d8e4f316'
down_revision: Union[str, Sequence[str], None] = 'a4e7b2f9c105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('digest_recipient_email', sa.String(320), nullable=True))


def downgrade() -> None:
    op.drop_column('venues', 'digest_recipient_email')
