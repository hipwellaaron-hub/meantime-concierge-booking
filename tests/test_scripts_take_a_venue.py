"""A script that writes bookings must be told which venue.

Every import/reconcile script opened with
`db.query(Venue).filter_by(slug="hamilton").one()`, so it wrote whatever CSV
you handed it into whichever venue somebody had written into the source.
Invisible with one venue; with two it is an import of one company's
functions into the other's books -- and `bookings.venue_id` is immutable by
database trigger, so the remedy is delete-and-recreate after clients already
hold their reference codes, not an UPDATE.

Required, never defaulted: a script that guesses a venue is the same class of
mistake as a payment link minted with a guessed key. It succeeds, silently,
into the wrong company.
"""
import pytest

from app.models import Venue
from app.venue_arg import VenueArgumentError, resolve_venue, take_venue_arg

USAGE = "Usage: python -m app.something --venue <slug> <path>"


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected_slug, expected_rest",
    [
        (["--venue", "hamilton", "a.csv"], "hamilton", ["a.csv"]),
        (["--venue=hamilton", "a.csv"], "hamilton", ["a.csv"]),
        (["a.csv", "--venue", "entrance"], "entrance", ["a.csv"]),
        (["--report", "a.csv", "--venue", "hamilton"], "hamilton", ["--report", "a.csv"]),
        (["a.csv", "b.csv"], None, ["a.csv", "b.csv"]),
    ],
)
def test_the_venue_argument_is_pulled_out_wherever_it_sits(argv, expected_slug, expected_rest):
    """It must not have to come first: --report already occupies that slot on
    the migration script, and a positional path is what people type first."""
    slug, rest = take_venue_arg(argv)

    assert slug == expected_slug
    assert rest == expected_rest


def test_a_dangling_venue_flag_is_refused():
    with pytest.raises(VenueArgumentError, match="needs a slug"):
        take_venue_arg(["a.csv", "--venue"])


# --- resolution ------------------------------------------------------------


def test_a_named_venue_resolves(db, hamilton):
    assert resolve_venue(db, "hamilton", usage=USAGE) is hamilton


def test_no_venue_argument_REFUSES_rather_than_defaulting(db, hamilton):
    """THE guard. There is no correct venue to guess."""
    with pytest.raises(VenueArgumentError) as exc:
        resolve_venue(db, None, usage=USAGE)

    assert "--venue is required" in str(exc.value)
    assert "hamilton" in str(exc.value), "the refusal must say what is available"


def test_an_unknown_slug_lists_the_real_ones(db, hamilton):
    """The most likely reason for getting here is a typo, and the second is
    not knowing the spelling. Both are answered by naming them."""
    with pytest.raises(VenueArgumentError) as exc:
        resolve_venue(db, "hamiltn", usage=USAGE)

    assert "hamiltn" in str(exc.value)
    assert "hamilton" in str(exc.value)


def test_a_second_venue_is_reachable_by_name(db, hamilton):
    """The point of the argument: the other venue must be selectable."""
    other = Venue(name="The Entrance", slug="entrance", reference_prefix="ENT")
    db.add(other)
    db.flush()

    assert resolve_venue(db, "entrance", usage=USAGE) is other
    assert resolve_venue(db, "hamilton", usage=USAGE) is hamilton


def test_no_script_hardcodes_a_venue_any_more():
    """The sweep. These four scripts write or report on bookings in bulk and
    none of them may pick a venue for you."""
    import pathlib

    offenders = []
    for name in ("run_ivvy_import", "run_ivvy_reconcile", "run_concierge_migration", "send_digest"):
        source = pathlib.Path(f"app/{name}.py").read_text(encoding="utf-8")
        # Ignore prose; only a real query counts.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if 'filter_by(slug=' in line or 'Venue.slug ==' in line:
                offenders.append(f"{name}: {stripped}")

    assert not offenders, offenders


def test_the_one_off_env_importer_is_gone():
    """app/run_ivvy_import_from_env.py read real customer PII out of
    base64 environment variables for a one-time production import that has
    already run (the iVvy cutover, 2026-09-01). Its own docstring said to
    remove it afterwards. It was also the only script whose venue could not
    be passed at all, because it took no arguments."""
    import pathlib

    assert not pathlib.Path("app/run_ivvy_import_from_env.py").exists()


# --- the deploy says what is missing ---------------------------------------


def test_the_seed_reports_a_fully_configured_venue(db, hamilton):
    from app.seed import report_gaps, unfilled_columns

    assert unfilled_columns(hamilton) == []
    assert "every client-facing column is filled" in report_gaps(hamilton)


def test_the_seed_names_every_unfilled_client_facing_column(db, hamilton):
    """With no fallback anywhere, a blank column is a blank on an invoice or
    a contract. That is the right trade only if somebody finds out, and
    preDeploy's entire output used to be "Seeded Hamilton venue and
    spaces"."""
    from app.seed import report_gaps, unfilled_columns

    hamilton.abn = None
    hamilton.bank_bsb = None
    db.flush()

    missing = unfilled_columns(hamilton)
    line = report_gaps(hamilton)

    assert set(missing) == {"abn", "bank_bsb"}
    assert "WARNING" in line
    assert "abn" in line and "bank_bsb" in line
    assert "print BLANK on client documents" in line


def test_an_unset_stripe_account_id_is_not_reported_as_a_gap(db, hamilton):
    """It is expected to be NULL until somebody arms the credential guard by
    hand -- its own numbered step in the go-live checklist. Reporting it
    every deploy would train the reader to ignore the line."""
    from app.seed import unfilled_columns

    hamilton.stripe_account_id = None
    db.flush()

    assert "stripe_account_id" not in unfilled_columns(hamilton)
