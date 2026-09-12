"""One pending Event Order proposal per booking, held by the database.

`uq_beo_proposal_one_pending_per_booking` is a PARTIAL unique index --
unique on booking_id WHERE status = 'pending' -- created by a3f6e1c7d094.
app/services/beo_proposal.py relies on it by name in two places as the thing
that closes the race when two writers propose at once; superseding the older
proposal in Python cannot do that on its own.

WHY THIS TEST EXISTS. The index is not declared on the model, so alembic's
autogenerate proposes DROPPING it on every run (see the standing note in
alembic/env.py). Accepting that proposal would retire the rule in silence:
no error, no failing test anywhere else, and a booking could carry two
pending proposals -- one of which the UI would never show. This test makes
that a red build instead.

It writes through the MODEL, deliberately, not through
beo_proposal.propose(). The service supersedes the older proposal itself, so
a test that went through it would pass with the index dropped -- it would be
measuring the service, not the guarantee.
"""
import datetime as dt
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.beo_proposal import STATUS_PENDING, BeoProposal


def _pending(booking_id):
    return BeoProposal(
        id=uuid.uuid4(),
        booking_id=booking_id,
        status=STATUS_PENDING,
        source="test: the database's own rule",
        created_by="test",
    )


def test_a_second_pending_proposal_is_refused_by_the_database(db, booking):
    db.add(_pending(booking.id))
    db.flush()

    db.add(_pending(booking.id))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_the_index_is_partial_so_resolved_proposals_do_not_block_a_new_one(db, booking):
    """The other half, and the reason it is partial rather than a plain
    unique constraint: a booking gets many proposals over its life, just
    never two PENDING at once. A unique index that was not partial would
    pass the test above while making the second ask of the evening
    impossible."""
    first = _pending(booking.id)
    db.add(first)
    db.flush()

    first.status = "resolved"
    first.resolved_at = dt.datetime.now(dt.timezone.utc)
    db.flush()

    db.add(_pending(booking.id))
    db.flush()  # must not raise

    assert db.query(BeoProposal).filter_by(booking_id=booking.id).count() == 2
