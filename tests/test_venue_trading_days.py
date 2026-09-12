"""Which days a venue trades is data, not three hardcoded copies.

Aaron, 2026-09-12: The Entrance is closed Monday and Tuesday, same as
Hamilton. Before migration a4e7b2f9c105 that fact lived in
venue_profile.py's literal, in floor.html's `dow === 1 || dow === 2`, and in
floor.html's "Mon & Tue closed" legend -- and a venue with different days
would have needed all three found.
"""
import pytest

from app.models import Venue
from app.services import venue_profile


def test_hamiltons_row_says_wednesday_to_sunday(db, hamilton):
    """Python weekday numbers, Monday=0 -- the same numbering date.weekday()
    returns, so a check needs no conversion.

    Named for the ROW, not the seed: migration a4e7b2f9c105 backfills this
    value, and app/seed.py fills only what is empty, so on any database that
    has been migrated the seed never gets to apply its copy. The first
    version of this test was called ...is_seeded... and passed with the
    seed's value replaced by None, which is a test proving the migration
    while claiming to prove the seed. The seed's half is covered below.
    """
    assert hamilton.trading_days == [2, 3, 4, 5, 6]


def test_the_seed_fills_trading_days_when_the_row_has_none(db, hamilton):
    """The seed's half, which only fires on a row that predates the column
    or a database built without migrating -- the case the seed exists for."""
    from app.seed import seed

    hamilton.trading_days = None
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.trading_days == [2, 3, 4, 5, 6], "the seed left a blank trading week blank"


def test_the_seed_does_not_overwrite_a_changed_trading_week(db, hamilton):
    """And the other half of the seed's rule: a week somebody set in the
    database is somebody's answer, and a deploy must not revert it."""
    from app.seed import seed

    hamilton.trading_days = [0, 1, 2, 3, 4, 5, 6]
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.trading_days == [0, 1, 2, 3, 4, 5, 6], "a deploy reverted the trading week"


def test_the_sentence_is_built_from_the_column(db, hamilton):
    """And for Hamilton it comes out byte-identical to the literal it
    replaced, which is why this was safe to change now."""
    assert venue_profile.closed_days_text(hamilton) == "closed Monday and Tuesday"
    assert venue_profile.for_venue(hamilton).closed_days_text == "closed Monday and Tuesday"


@pytest.mark.parametrize(
    "trading_days, expected",
    [
        ([2, 3, 4, 5, 6], "closed Monday and Tuesday"),
        ([0, 1, 2, 3, 4, 5, 6], "open seven days"),
        ([0, 1, 2, 3, 4, 5], "closed Sunday"),
        ([4, 5], "closed Monday, Tuesday, Wednesday, Thursday and Sunday"),
    ],
)
def test_the_sentence_reads_correctly_for_any_week(trading_days, expected):
    venue = Venue(name="Probe", slug="probe")
    venue.trading_days = trading_days

    assert venue_profile.closed_days_text(venue) == expected


def test_a_venue_that_has_not_recorded_its_days_says_nothing(db, hamilton):
    """NULL means nobody has said. It must NOT be read as "same week as
    Hamilton" -- that is the guess that puts a function on a day the kitchen
    is shut. closed_days_text returns None and for_venue keeps the profile's
    own text rather than inventing one.
    """
    hamilton.trading_days = None
    db.flush()

    assert venue_profile.closed_days_text(hamilton) is None
    # The profile literal still stands in; nothing is fabricated from the row.
    assert venue_profile.for_venue(hamilton).closed_days_text == "closed Monday and Tuesday"


def test_the_column_overrides_the_profile_literal(db, hamilton):
    """The point of the column: when the row and the literal disagree, the
    row wins. Otherwise a venue could not change its days without a deploy."""
    hamilton.trading_days = [0, 1, 2, 3, 4, 5, 6]
    db.flush()

    assert venue_profile.for_venue(hamilton).closed_days_text == "open seven days"


def test_the_drafting_prompt_carries_the_row_not_the_literal(db, hamilton):
    """What a client would actually be told. The prompt is built per booking
    from for_venue, so a changed row reaches the sentence the AI writes."""
    from app.services.drafting import build_system_prompt

    hamilton.trading_days = [0, 1, 2, 3, 4, 5, 6]
    db.flush()

    prompt = build_system_prompt(venue_profile.for_venue(hamilton))

    assert "open seven days" in prompt
    assert "closed Monday and Tuesday" not in prompt
