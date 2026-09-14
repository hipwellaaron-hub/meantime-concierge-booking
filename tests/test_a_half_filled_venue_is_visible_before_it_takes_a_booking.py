"""A venue with unfilled client-facing columns says so, at runtime, in both places.

The list of columns a client reads has existed since 2026-09-12, with no
fallback for any of them on purpose: nothing substitutes another company's
bank details onto a document. What was missing is WHEN anybody finds out.

`python -m app.seed` reported the gaps -- once, at deploy, into a log line
read by whoever ran the deploy on the day they ran it. A second venue's row
is typed in by hand, days after that deploy. Nothing asked again.

So the same list is now read at runtime, twice:

  * /healthz, folded to one boolean, DEGRADING -- an unfilled column is not
    a decision anybody made, it is a document that will go out wrong.
  * the 20:30 digest, by name, because Aaron's rule (2026-09-14) is that a
    check that fires without reaching the digest is not finished.

Off the SAME seed.unfilled_columns, so the deploy log, the endpoint and the
email cannot disagree about whether a venue is ready.

AND reference_prefix is called out separately, because it does not print
blank -- it REFUSES. booking.generate_reference_code raises ValueError and
migration f3d9b7c1a468's invoice trigger RAISEs in Postgres, so a venue
without it cannot take a booking or issue an invoice at all. That is the
one gap that would otherwise be found as a 500 on the first real enquiry.
"""
import pytest

from app import seed
from app.services import digest


def _healthz(db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        return TestClient(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()


# --- the endpoint -------------------------------------------------------


def test_a_fully_filled_venue_reads_ready(db, hamilton):
    """The positive control. Hamilton is seeded complete, so without this
    every probe below could be passing on an endpoint that always says
    'not ready'."""
    assert seed.unfilled_columns(hamilton) == [], (
        "the seeded venue is already incomplete -- fix the fixture, or the "
        "probes below prove nothing about the code"
    )

    body = _healthz(db)

    assert body["checks"]["venues_client_ready"] is True


@pytest.mark.parametrize("column", ["abn", "bank_bsb", "contact_email", "reference_prefix"])
def test_one_unfilled_column_reads_not_ready_and_degrades(db, hamilton, column):
    setattr(hamilton, column, None)
    db.flush()

    body = _healthz(db)

    assert body["checks"]["venues_client_ready"] is False, (
        f"{column} is unfilled and /healthz still reports the venue ready"
    )
    assert body["status"] == "degraded"


def test_an_empty_string_counts_as_unfilled(db, hamilton):
    """A hand-typed row gets '' far more often than NULL, and `not ''` is
    what the seed helper tests. Asserted because a later rewrite reaching
    for `is None` would read a blank ABN as filled."""
    hamilton.abn = "   "
    db.flush()

    assert _healthz(db)["checks"]["venues_client_ready"] is False


def test_a_second_venue_is_asked_too(db, hamilton):
    """The failure this exists for, and the one two weeks away. Hamilton is
    complete; the venue that is half-filled is the NEW one, which is exactly
    the one a check that only ever looked at the first venue would miss.

    Built complete and then broken, so the probe fails on the missing
    column rather than on the second venue merely existing."""
    from app.models import Venue

    entrance = Venue(name="Meantime The Entrance", slug="entrance")
    for column in seed.CLIENT_FACING_COLUMNS:
        setattr(entrance, column, getattr(hamilton, column))
    entrance.reference_prefix = "ENT"
    db.add(entrance)
    db.flush()

    assert _healthz(db)["checks"]["venues_client_ready"] is True, (
        "a complete second venue already reads not-ready -- the probe below "
        "would pass for the wrong reason"
    )

    # BOTH WAYS ROUND, and that is the whole point of the probe. Nothing
    # orders the venues, so breaking only one of them and asserting once
    # passes whenever the broken one happens to be the row a
    # single-venue implementation looks at -- which is exactly how this
    # probe passed against `venues[0]` on the first run. One of these two
    # must fail for any implementation that asks fewer than all of them.
    entrance.reference_prefix = None
    db.flush()
    broke_the_second = _healthz(db)["checks"]["venues_client_ready"]

    entrance.reference_prefix = "ENT"
    hamilton.reference_prefix = None
    db.flush()
    broke_the_first = _healthz(db)["checks"]["venues_client_ready"]

    assert broke_the_second is False, (
        "a complete Hamilton folded a half-filled second venue into 'ready'"
    )
    assert broke_the_first is False, (
        "a complete second venue folded a half-filled Hamilton into 'ready'"
    )


def test_the_endpoint_names_no_venue_and_no_column(db, hamilton):
    """Public endpoint. Which company is half set up, and which of its
    details are missing, is not a fact a monitoring URL hands out -- the
    detail goes to the log and to Aaron's digest."""
    hamilton.bank_account_number = None
    db.flush()

    blob = repr(_healthz(db)).lower()

    for leak in ("hamilton", "bank_account_number", "abn", "meantime"):
        assert leak not in blob, f"/healthz leaks {leak!r}"


def test_the_gaps_reach_the_log(db, hamilton, caplog):
    """A boolean with no detail anywhere is a check nobody can act on."""
    import logging

    hamilton.abn = None
    db.flush()

    with caplog.at_level(logging.WARNING, logger="app.api.health"):
        _healthz(db)

    assert any("abn" in r.getMessage() for r in caplog.records), (
        "the endpoint reports not-ready and logs nothing about which column"
    )


# --- the digest ---------------------------------------------------------


def test_the_digest_names_the_gaps(db, hamilton):
    hamilton.abn = None
    hamilton.bank_bsb = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "abn" in body and "bank_bsb" in body, (
        "the digest does not name the unfilled columns"
    )
    assert content.venue_gaps, "build_digest did not collect the gaps at all"


def test_a_complete_venue_puts_nothing_in_the_digest(db, hamilton):
    """Self-clearing, like every other section: fill the column and the
    section is simply absent tomorrow, with nothing to mark as sent."""
    content = digest.build_digest(db, hamilton)

    assert content.venue_gaps == []
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")
    assert "SET-UP INCOMPLETE" not in body


def test_reference_prefix_is_called_out_as_blocking(db, hamilton):
    """It does not print blank, it refuses. A line that lumps it in with
    the ABN reads as cosmetic."""
    hamilton.reference_prefix = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "cannot take a booking" in body, (
        "reference_prefix is listed as if it merely printed blank"
    )


def test_a_blank_column_that_only_prints_blank_is_not_called_blocking(db, hamilton):
    """The other half of the discriminator: without this, a line that said
    'cannot take a booking' unconditionally would pass the probe above."""
    hamilton.abn = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "cannot take a booking" not in body


def test_the_section_is_one_item_not_one_per_column(db, hamilton):
    """Fourteen unfilled columns on a venue nobody has set up yet is one
    job for one person. Counting them individually would put '15 items
    need attention' in the subject over a single new venue and bury the
    bookings under it."""
    hamilton.abn = None
    hamilton.bank_bsb = None
    hamilton.phone = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    subject, _ = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert len(content.venue_gaps) == 3
    assert content.item_count == 1, "three columns counted as three items"
    assert "1 item" in subject


def test_the_section_comes_before_the_bookings(db, hamilton, booking):
    """A venue that cannot produce a correct document outranks a reminder
    about one -- and if reference_prefix is the gap, every section below is
    empty for the wrong reason."""
    hamilton.reference_prefix = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    content.wizard_eligible = [booking]
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "SET-UP INCOMPLETE" in body and "WIZARD" in body
    assert body.index("SET-UP INCOMPLETE") < body.index("WIZARD"), (
        "the set-up section renders below the bookings"
    )


def test_an_incomplete_venue_is_never_an_all_clear(db, hamilton):
    """is_empty drives the 'all clear' subject. A venue that cannot take a
    booking reading 'all clear' is the exact shape of the deploy log this
    replaces."""
    hamilton.reference_prefix = None
    db.flush()

    content = digest.build_digest(db, hamilton)
    subject, _ = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert content.is_empty is False
    assert "all clear" not in subject


def test_the_digest_and_the_endpoint_read_the_same_list(db, hamilton):
    """Structural, and deliberately so: the two cannot be proved equal by
    behaviour alone, because any single column would satisfy both. What
    matters is that there is ONE list -- a second copy of 'which columns
    matter' is how a deploy log comes to say ready while an email says not."""
    import inspect

    from app.api import health

    endpoint_src = inspect.getsource(health.healthz)
    digest_src = inspect.getsource(digest.build_digest)

    assert "seed.unfilled_columns" in endpoint_src
    assert "unfilled_columns(venue)" in digest_src
