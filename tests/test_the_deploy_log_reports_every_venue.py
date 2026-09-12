"""Every venue's unfilled columns reach the deploy log.

`app/seed.py` runs on every deploy (preDeploy) and its whole output used to
be one line: "Seeded Hamilton venue and spaces." The gap report was added so
a venue missing an ABN or a bank account could not reach a client invoice
unnoticed -- but it was called as `report_gaps(seed())`, and `seed()`
creates and returns the Hamilton row and nothing else.

So the check aimed at a half-filled venue could only ever look at the one
venue that was filled in by code. The venue actually likely to have gaps is
the new one, typed in by hand, and it was the one the report never saw.

There is no fallback behind these columns on purpose: a blank prints blank
rather than substituting another company's details. That is the right trade
only if somebody finds out.
"""
import pytest

from app.models import Venue
from app.seed import CLIENT_FACING_COLUMNS, report_every_venue, report_gaps, unfilled_columns


@pytest.fixture()
def half_built(db, hamilton):
    """A venue row with a name and nothing else -- what a hand-typed second
    venue looks like a minute after it is inserted."""
    venue = Venue(name="The Entrance", slug="entrance", trading_name="Meantime The Entrance")
    db.add(venue)
    db.flush()
    return venue


def test_a_half_filled_second_venue_is_reported(db, hamilton, half_built):
    """THE regression. Before this, the report was handed Hamilton and only
    Hamilton, so this venue's blanks were invisible at deploy."""
    lines = report_every_venue(db)

    entrance_lines = [l for l in lines if "The Entrance" in l]
    assert entrance_lines, f"the second venue was not reported at all: {lines}"
    assert "WARNING" in entrance_lines[0]
    for column in ("abn", "bank_bsb", "reference_prefix"):
        assert column in entrance_lines[0], f"{column} was not named as missing"


def test_the_filled_venue_is_still_reported_as_filled(db, hamilton, half_built):
    """Both venues get a line. A report that only printed problems would
    leave "did it even look at Hamilton?" unanswerable."""
    lines = report_every_venue(db)

    assert len(lines) == 2, lines
    hamilton_line = next(l for l in lines if "Hamilton" in l)
    assert "every client-facing column is filled" in hamilton_line


def test_every_reported_column_has_no_fallback_behind_it(db, hamilton, half_built):
    """The list is only useful if its members really do print blank. Guard
    against a column being added to CLIENT_FACING_COLUMNS that something
    else quietly fills in."""
    missing = unfilled_columns(half_built)

    assert set(missing) <= set(CLIENT_FACING_COLUMNS)
    for column in missing:
        assert not getattr(half_built, column, None), (
            f"{column} is reported as unfilled but has a value"
        )


def test_a_venue_with_nothing_missing_says_so(db, hamilton):
    assert report_gaps(hamilton) == f"{hamilton.trading_name}: every client-facing column is filled."
