"""Venue-scoped invoice queries read invoices.venue_id.

The column was written by a trigger, made NOT NULL, given a foreign key,
frozen against UPDATE and made half the per-venue unique -- and then no
SELECT in the application read it. All three venue-scoped invoice queries
reached through Booking -> Space instead.

That was not WRONG: the triggers make the two agree by construction. It was
worse than wrong. A column that is frozen, constrained and ignored reads as
scoping to whoever arrives next, which is the mislabelled-page shape one
layer down -- the scoping looks done and the query is somewhere else
entirely (Aaron, 2026-09-13).

TESTING THIS HONESTLY IS AWKWARD, and worth saying out loud: because the
two paths agree by construction, no black-box test can tell which one ran.
A test that created an invoice and asserted it appeared would pass either
way. So there are two halves here:

  * BEHAVIOURAL tests that the scoping is right in both directions, which
    would catch a broken predicate; and
  * a STRUCTURAL test that the join is actually gone, which is the only
    thing that can catch a silent revert to the old shape.
"""
import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.models import Space, Venue
from app.models.invoice import InvoiceStatus
from app.services import invoicing
from app.services.booking import create_booking

DUE = dt.date.today() + dt.timedelta(days=7)


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


def _invoice_at(db, venue, name, *, status=None):
    booking = create_booking(
        db, space_id=venue.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=55), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    invoice = invoicing.create_deposit_invoice(db, booking, due_date=DUE, actor="test")
    if status is not None:
        invoice.status = status
    db.flush()
    return invoice


# --- the scoping, in both directions ---------------------------------------


def test_the_register_lists_only_this_venues_invoices(db, hamilton, loft, entrance):
    ham = _invoice_at(db, hamilton, f"ZZHAM {uuid.uuid4().hex[:6]}")
    ent = _invoice_at(db, entrance, f"ZZENT {uuid.uuid4().hex[:6]}")

    ham_list = invoicing.search_invoices(db, venue_id=hamilton.id)
    ent_list = invoicing.search_invoices(db, venue_id=entrance.id)

    assert ham.id in [i.id for i in ham_list]
    assert ent.id not in [i.id for i in ham_list], "another venue's invoice was on this register"
    assert ent.id in [i.id for i in ent_list]
    assert ham.id not in [i.id for i in ent_list]


def test_the_register_is_ordered_by_its_own_number(db, hamilton, loft):
    """created_at is now(), which Postgres fixes at TRANSACTION START,
    while the number is taken later inside the insert -- so two invoices
    raised at the same moment could list in the opposite order to their
    numbers, silently. A register ordered by its own position cannot."""
    for n in range(3):
        _invoice_at(db, hamilton, f"ZZORDER {n} {uuid.uuid4().hex[:6]}")

    numbers = [i.invoice_number for i in invoicing.search_invoices(db, venue_id=hamilton.id)]

    assert numbers == sorted(numbers, reverse=True), f"the register listed out of order: {numbers}"


def test_the_overdue_digest_covers_only_its_own_venue(db, hamilton, loft, entrance):
    from app.services.digest import get_overdue_invoices

    overdue_date = dt.date.today() - dt.timedelta(days=5)
    ham = _invoice_at(db, hamilton, f"ZZHAMOVERDUE {uuid.uuid4().hex[:6]}", status=InvoiceStatus.sent)
    ent = _invoice_at(db, entrance, f"ZZENTOVERDUE {uuid.uuid4().hex[:6]}", status=InvoiceStatus.sent)
    ham.due_date = overdue_date
    ent.due_date = overdue_date
    db.flush()

    ham_overdue = get_overdue_invoices(db, hamilton)
    ent_overdue = get_overdue_invoices(db, entrance)

    assert ham.invoice_reference in [o.invoice.invoice_reference for o in ham_overdue]
    assert ent.invoice_reference not in [o.invoice.invoice_reference for o in ham_overdue], (
        "one venue's digest chased the other company's client"
    )
    assert ent.invoice_reference in [o.invoice.invoice_reference for o in ent_overdue]


def test_the_dashboards_unpaid_count_is_this_venues(admin_client, db, hamilton, loft, entrance):
    """The count and the list it links to must agree; they now read the
    same column, which is the point."""
    _invoice_at(db, hamilton, f"ZZHAMSENT {uuid.uuid4().hex[:6]}", status=InvoiceStatus.sent)
    _invoice_at(db, entrance, f"ZZENTSENT {uuid.uuid4().hex[:6]}", status=InvoiceStatus.sent)
    _invoice_at(db, entrance, f"ZZENTSENT2 {uuid.uuid4().hex[:6]}", status=InvoiceStatus.sent)

    listed = invoicing.search_invoices(db, venue_id=entrance.id, status=InvoiceStatus.sent)

    assert len(listed) == 2, "the register and the tile would disagree"
    assert all(i.venue_id == entrance.id for i in listed)


# --- the structural half ----------------------------------------------------


def test_no_venue_scoped_invoice_query_still_joins_through_space():
    """The only thing that can catch a silent revert.

    The two paths agree by construction -- the insert trigger takes
    venue_id from the booking and a freeze trigger holds it there -- so a
    behavioural test cannot tell which one ran. This can.

    Matched by AST rather than by text, because the docstrings in these
    functions DESCRIBE the join they used to do, and a grep would match its
    own explanation. That exact mistake has been made here before.

    Keyed on the STATEMENT rather than the enclosing function: the admin
    dashboard is one long function that legitimately scopes its BOOKING
    queries through Space, and a function-level check flagged those as
    invoice defects on its first run.
    """
    import ast
    import pathlib

    offenders = []
    for path in (
        "app/services/invoicing.py",
        "app/services/digest.py",
        "app/api/admin_dashboard.py",
    ):
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        for stmt in ast.walk(tree):
            # PER STATEMENT, not per function. admin_dashboard.dashboard()
            # is one long function that legitimately scopes BOOKING queries
            # through Space; keying on the function caught those too and
            # reported a defect that was not there.
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
                continue
            names = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
            if "Invoice" not in names:
                continue
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr == "venue_id"
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "Space"
                ):
                    offenders.append(f"{path}:{sub.lineno}")

    assert not offenders, (
        "these invoice queries still scope through Space rather than reading "
        f"invoices.venue_id: {offenders}"
    )


def test_the_structural_check_can_actually_see_one():
    """The premise the check above rests on. A walker that stopped matching
    would report clean over everything -- the same failure in a new
    costume."""
    import ast

    sample = "\n".join([
        "def listed(db, venue_id):",
        "    'Scoped through Booking -> Space, as the docstring says.'",
        "    return select(Invoice).join(Space).where(Space.venue_id == venue_id)",
    ])
    tree = ast.parse(sample)
    found = []
    for stmt in ast.walk(tree):
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
            continue
        names = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        if "Invoice" not in names:
            continue
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr == "venue_id"
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "Space"
            ):
                found.append(sub.lineno)

    assert found, "the walker found nothing in a sample that plainly has one"
