"""The nightly reconciliation run covers every venue.

`--venue` defaulted to `settings.ai_venue_slug`, which is not a venue slug:
`app/services/ai_access.py` splits it on commas as the list of venues the AI
credential is ALLOWED to read. With one venue the two happened to coincide.
With two they do not, and the failure is in the worst direction available --
the unattended run matches no venue at all and reconciles NEITHER, leaving
both venues' triage findings lists empty. An empty findings list is
indistinguishable from a clean one.

So the default is now every venue, the way the digest does it, and
`--venue` narrows rather than selects.
"""
import datetime as dt

import pytest
from decimal import Decimal

from app.config import settings
from app.models import Contact, ReconciliationFinding, Space, Venue
from app.models.booking import BookingStatus
from app.services.booking import change_status, create_booking

TODAY = dt.date.today()


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        reference_prefix="ENT",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


@pytest.fixture()
def run_against_the_test_session(db, monkeypatch):
    """main() opens its own session. Point that at the test transaction so
    the whole entry point runs, argument parsing and all -- calling
    reconciliation.run() directly would test the service and leave the
    runner, which is where the bug was, untested."""
    import app.run_reconciliation as runner

    class _Session:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(runner, "SessionLocal", lambda: _Session())
    return runner


def _flagged_booking(db, space, name, email):
    """A confirmed booking whose contact has no email -- CONFIRMED_NO_EMAIL,
    the cheapest real finding to produce."""
    contact = Contact(name=name, email=f"{email}")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id,
        event_date=TODAY + dt.timedelta(days=60), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="staff:test",
    )
    contact.email = ""
    change_status(db, booking, BookingStatus.confirmed, actor="staff:test")
    db.flush()
    return booking


def _slugs_with_findings(db):
    from app.models import Booking

    out = set()
    for finding in db.query(ReconciliationFinding).all():
        booking = db.get(Booking, finding.booking_id)
        if booking is not None:
            out.add(booking.venue.slug)
    return out


def test_a_bare_run_reconciles_every_venue(
    run_against_the_test_session, db, hamilton, loft, entrance, monkeypatch, capsys
):
    """THE regression. Both venues have something to find; both must be
    looked at."""
    _flagged_booking(db, loft, "ZZHAM Recon", "zzham.recon@example.com")
    _flagged_booking(db, entrance.spaces[0], "ZZENT Recon", "zzent.recon@example.com")
    monkeypatch.setattr("sys.argv", ["run_reconciliation"])

    assert run_against_the_test_session.main() == 0

    assert _slugs_with_findings(db) == {"hamilton", "entrance"}, (
        "the nightly run left a venue unreconciled, which reads as 'all clear'"
    )
    out = capsys.readouterr().out
    assert "--- hamilton ---" in out and "--- entrance ---" in out, (
        "each venue must be named in the log, or a partial run is invisible"
    )


def test_the_ai_allowlist_is_not_consulted(
    run_against_the_test_session, db, hamilton, loft, entrance, monkeypatch
):
    """The specific bug, pinned. AI_VENUE_SLUG is a comma-separated
    ALLOWLIST; it was being handed to a `slug ==` lookup. With the real
    two-venue value it matched nothing and the job reconciled nothing.

    Set to a value that is not any venue's slug: the run must be completely
    indifferent to it."""
    monkeypatch.setattr(settings, "ai_venue_slug", "hamilton,entrance")
    _flagged_booking(db, loft, "ZZHAM Allowlist", "zzham.allow@example.com")
    _flagged_booking(db, entrance.spaces[0], "ZZENT Allowlist", "zzent.allow@example.com")
    monkeypatch.setattr("sys.argv", ["run_reconciliation"])

    assert run_against_the_test_session.main() == 0
    assert _slugs_with_findings(db) == {"hamilton", "entrance"}


def test_naming_one_venue_still_narrows_to_it(
    run_against_the_test_session, db, hamilton, loft, entrance, monkeypatch
):
    """The other direction, so 'always do everything' could not pass the
    test above on its own."""
    _flagged_booking(db, loft, "ZZHAM Only", "zzham.only@example.com")
    _flagged_booking(db, entrance.spaces[0], "ZZENT Untouched", "zzent.untouched@example.com")
    monkeypatch.setattr("sys.argv", ["run_reconciliation", "--venue", "hamilton"])

    assert run_against_the_test_session.main() == 0
    assert _slugs_with_findings(db) == {"hamilton"}


def test_an_unknown_slug_is_refused_and_lists_the_real_ones(
    run_against_the_test_session, db, hamilton, entrance, monkeypatch, capsys
):
    """It used to print 'No venue with slug ...' and exit 1 -- correct, but
    with no way to find the right spelling. It now names them."""
    monkeypatch.setattr("sys.argv", ["run_reconciliation", "--venue", "hamiltn"])

    assert run_against_the_test_session.main() == 1
    out = capsys.readouterr().out
    assert "hamilton" in out and "entrance" in out


def test_a_venue_that_raises_does_not_cost_the_other_its_run(
    run_against_the_test_session, db, hamilton, loft, entrance, monkeypatch, capsys
):
    """Two companies on one cron. One of them blowing up must not silently
    take the other's nightly check with it."""
    from app.services import reconciliation

    _flagged_booking(db, loft, "ZZHAM Survives", "zzham.survives@example.com")
    real_run = reconciliation.run

    def _explode_for_entrance(session, venue, *a, **kw):
        if venue.slug == "entrance":
            raise RuntimeError("boom")
        return real_run(session, venue, *a, **kw)

    monkeypatch.setattr(reconciliation, "run", _explode_for_entrance)
    monkeypatch.setattr("sys.argv", ["run_reconciliation"])

    assert run_against_the_test_session.main() == 1, "a failed venue must exit non-zero"
    assert _slugs_with_findings(db) == {"hamilton"}, "the healthy venue was still reconciled"


def test_no_venues_at_all_is_a_failure_not_a_quiet_success(monkeypatch, capsys):
    """A reconciliation that found no venue to reconcile has not run. Exit 0
    there would report a clean night to whatever watches the cron.

    Uses a stub session rather than the test database, because the test
    database always has Hamilton committed in it -- a test that tried to
    produce "no venues" by not asking for the fixture would be asserting
    against a venue that is there anyway.
    """
    import app.run_reconciliation as runner

    class _Empty:
        def scalars(self, *_a, **_kw):
            return self

        def all(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(runner, "SessionLocal", lambda: _Empty())
    monkeypatch.setattr("sys.argv", ["run_reconciliation"])

    assert runner.main() == 1
    assert "nothing was reconciled" in capsys.readouterr().out
