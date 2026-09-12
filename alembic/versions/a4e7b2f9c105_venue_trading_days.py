"""a venue records which days it trades

Aaron, 2026-09-12, answering the open question rather than deferring it:
The Entrance is closed Monday and Tuesday, same as Hamilton.

Until now "closed Monday and Tuesday" existed in three places and none of
them was data:

  * app/services/venue_profile.py -- the sentence the AI drafter says
  * app/templates/floor/floor.html:328 -- `dow === 1 || dow === 2`, which
    greys those cells on the floor calendar
  * app/templates/floor/floor.html:338 -- the legend "Mon & Tue closed"

Three copies of one fact, and a second venue with different days would have
needed all three found. This is the column they collapse onto.

SHAPE. A smallint array of the days the venue is OPEN, using PYTHON's
weekday numbering (Monday=0 ... Sunday=6), which is what `date.weekday()`
returns -- so a server-side check is `d.weekday() in venue.trading_days`
with no conversion. JavaScript's getDay() is Sunday=0, and the floor grid
already converts with `(getDay() + 6) % 7`; the floor moves onto this
column in the floor slab, not here.

OPEN days rather than closed ones: the list is then non-empty for every
real venue, so NULL unambiguously means "nobody has said", and an empty
array means "open no days", which is a different and equally sayable thing.

NULLABLE, and deliberately not defaulted. A venue whose days nobody has
recorded must not be assumed to trade the same week as Hamilton -- that is
the class of guess that puts a function on a day the kitchen is shut.

Revision ID: a4e7b2f9c105
Revises: f2a9d5c81b64
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a4e7b2f9c105'
down_revision: Union[str, Sequence[str], None] = 'f2a9d5c81b64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'venues',
        sa.Column('trading_days', postgresql.ARRAY(sa.SmallInteger()), nullable=True),
    )
    # Hamilton: Wednesday through Sunday. Matches the sentence in
    # venue_profile.py ("closed Monday and Tuesday") and the floor
    # calendar's `dow === 1 || dow === 2`, both of which predate this column.
    op.execute(
        "UPDATE venues SET trading_days = ARRAY[2,3,4,5,6]::smallint[] WHERE slug = 'hamilton'"
    )


def downgrade() -> None:
    op.drop_column('venues', 'trading_days')
