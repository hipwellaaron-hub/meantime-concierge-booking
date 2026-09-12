"""Two inventories that make an omission visible before it ships.

Step 3 of the venue switch. Neither of these tests asserts that the scoping
is CORRECT -- they assert that nothing has been left un-considered. That is a
weaker claim and a much more durable one: a test that checks a list of things
somebody wrote down goes stale the moment somebody adds a thing and forgets
to write it down. These fail instead.

The route walker is the load-bearing part. `app.routes` holds 6 APIRoutes and
20 `_IncludedRouter` wrappers, so a naive `isinstance(r, APIRoute)` sweep
inspects 6 routes out of 112 and passes green over everything it never saw.
"""
import pathlib

from fastapi.routing import APIRoute

from app.database import Base
from app.main import app

# ---------------------------------------------------------------------------
# 1. Every route, actually enumerated
# ---------------------------------------------------------------------------

SNAPSHOT = pathlib.Path("tests/route_inventory.txt")


def _iter_api_routes(routes):
    """Recurse through _IncludedRouter wrappers to the real APIRoutes.

    FastAPI's include_router leaves a wrapper object on app.routes rather
    than splicing the child routes in, so the real routes are one level down
    on `.original_router.routes`. Miss that and a route-counting test
    silently covers 5% of the app.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_api_routes(inner.routes)


def _route_lines() -> list[str]:
    lines = set()
    for route in _iter_api_routes(app.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            lines.add(f"{method:6s} {route.path}")
    return sorted(lines)


def test_the_walker_actually_reaches_past_the_router_wrappers():
    """The premise everything below rests on. If this number collapses to
    single digits the walker has stopped recursing and every other route
    assertion has quietly become vacuous."""
    direct = [r for r in app.routes if isinstance(r, APIRoute)]
    walked = list(_iter_api_routes(app.routes))

    assert len(direct) < 10, "app.routes now exposes routes directly; re-check the walker"
    assert len(walked) > 100, (
        f"the walker found only {len(walked)} routes -- it is no longer recursing into "
        "_IncludedRouter, and every route test is now inspecting a handful of routes"
    )


def test_the_route_inventory_matches_its_snapshot():
    """A checked-in list of every route. This is not a rule about what routes
    may exist -- it is a diff that forces a new one to be looked at.

    When it fails: read the diff, decide whether the new route needs venue
    scoping, then update the snapshot in the same commit.

        python -c "from tests.test_venue_scope_inventory import write_snapshot; write_snapshot()"
    """
    current = _route_lines()

    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT} is missing -- regenerate it with write_snapshot()"
    )
    recorded = [l for l in SNAPSHOT.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]

    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))

    assert not added and not removed, (
        f"the route list changed.\n  ADDED:   {added}\n  REMOVED: {removed}\n"
        "Decide whether each added route needs venue scoping, then regenerate the snapshot."
    )


def write_snapshot() -> None:
    """Regenerate tests/route_inventory.txt. Deliberately not automatic:
    the point of the snapshot is that somebody looks at the diff."""
    header = (
        "# Every route in the app, enumerated by recursing _IncludedRouter.\n"
        "# Regenerate deliberately, after deciding whether a new route needs\n"
        "# venue scoping:\n"
        '#   python -c "from tests.test_venue_scope_inventory import write_snapshot; write_snapshot()"\n'
    )
    SNAPSHOT.write_text(header + "\n".join(_route_lines()) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Every mapped class, classified
# ---------------------------------------------------------------------------

# Rows that belong to exactly one venue and carry it themselves.
VENUE_SCOPED = {
    "Booking",  # venue_id, NOT NULL, immutable by trigger (d8c3f1a7e920)
    "Space",    # venue_id, NOT NULL -- the original scoped table
    # Step 8 (d6b4e9f2a831). Both NULLABLE, for different reasons:
    "StaffUser",      # NULL means EVERY venue -- an admin. The one place in
                      # this codebase a NULL venue means something.
    "StaffAppToken",  # NULL means the token predates the column or came from
                      # a rolled-back build, and is REFUSED rather than
                      # guessed. Nullable only so a rolled-back build can
                      # still issue tokens at all.
    # Two legal entities keep separate tax-invoice registers (f3d9b7c1a468),
    # so this stopped being reachable-through-booking and became a fact
    # about the row. NOT NULL, written only by a BEFORE INSERT trigger that
    # takes it from the booking, and frozen by a second trigger -- so it
    # cannot disagree with the booking it belongs to, and cannot move.
    "Invoice",
    # A venue's own running number. One row per venue; it IS the venue's
    # register.
    "VenueInvoiceCounter",
}

# Rows that do NOT carry a venue, each with the reason. A reason of the form
# "reached through X" means the row is scoped transitively and a query that
# forgets the join is the bug, not the schema.
VENUE_FREE = {
    "Venue": "is the venue",
    "AiSettings": "one row of process configuration; no venue dimension exists",
    "PublicHoliday": "NSW public holidays are a fact about the state, not a venue",
    "Contact": "a person is not owned by a venue (Aaron, 2026-09-12: shared, display scoped)",
    "MenuItem": "NOT YET SCOPED -- per-venue was settled 2026-09-12 and the column is not built",
    # Reached through Booking.
    "BookingEvent": "reached through booking_id",
    "BookingVendor": "reached through booking_id",
    "Document": "reached through booking_id",
    "EnquiryDraft": "reached through booking_id",
    "WizardSession": "reached through booking_id",
    "AiRequestLog": "reached through booking_id",
    "ConversionDispatch": "reached through booking_id",
    "ReconciliationFinding": "reached through booking_id",
    "BeoProposal": "reached through booking_id",
    # Reached one level deeper.
    "Payment": "reached through invoice_id -> booking_id",
    "BeoProposalField": "reached through beo_proposal_id -> booking_id",
}


def _mapped_class_names() -> set[str]:
    import app.models  # noqa: F401  -- registers every mapper

    return {m.class_.__name__ for m in Base.registry.mappers}


def test_every_model_is_classified():
    """A new model must be put in one bucket or the other, with a reason.

    This is the test that catches the table nobody thought about. It does not
    check that the classification is RIGHT -- it checks that somebody made
    one, which is the part that gets skipped.
    """
    classified = VENUE_SCOPED | set(VENUE_FREE)
    actual = _mapped_class_names()

    unclassified = sorted(actual - classified)
    stale = sorted(classified - actual)

    assert not unclassified, (
        f"these models are neither VENUE_SCOPED nor VENUE_FREE: {unclassified}. "
        "Decide which, and if VENUE_FREE say why in one line."
    )
    assert not stale, f"these are classified but no longer exist: {stale}"


def test_every_venue_scoped_model_really_has_the_column():
    """The classification is checked against the schema, not trusted."""
    import app.models  # noqa: F401

    by_name = {m.class_.__name__: m for m in Base.registry.mappers}
    missing = [
        name for name in VENUE_SCOPED
        if "venue_id" not in {c.key for c in by_name[name].columns}
    ]

    assert not missing, f"classified VENUE_SCOPED but carry no venue_id: {missing}"


def test_no_venue_free_model_quietly_grew_a_venue_column():
    """The other direction. A model that gains venue_id has become scoped,
    and its queries need revisiting -- that should not pass unnoticed."""
    import app.models  # noqa: F401

    by_name = {m.class_.__name__: m for m in Base.registry.mappers}
    grew = [
        name for name in VENUE_FREE
        if name in by_name and "venue_id" in {c.key for c in by_name[name].columns}
    ]

    assert not grew, (
        f"{grew} now carry venue_id but are classified VENUE_FREE -- move them to "
        "VENUE_SCOPED and check every query that reads them"
    )


def test_the_known_gaps_are_still_written_down():
    """The classification doubles as the list of what is NOT done. If one of
    these reasons is ever deleted without the work being finished, this says
    so."""
    assert "NOT YET SCOPED" in VENUE_FREE["MenuItem"], (
        "MenuItem's reason no longer records that per-venue pricing is unbuilt. "
        "If the column now exists, move it to VENUE_SCOPED."
    )
    # StaffUser's "accepted gap" reason is gone: step 8 scoped it, and the
    # gate that was watching for exactly this fired and was replaced by
    # tests/test_staff_is_scoped_to_a_venue.py.
    assert "StaffUser" not in VENUE_FREE, "StaffUser is scoped now; its gap note should be gone"
