"""Aaron's standing rule, 2026-09-14: "The nightly reconciliation at 20:30
and the digest after it are now how I find out something's wrong. If a
check fires and it doesn't reach the digest, that check isn't finished."

This is the proof of it, and it is deliberately TWO tests doing two
different jobs, because the honest answer has two halves.

THE STRUCTURAL HALF. The path from a check to the email has no per-check
branching anywhere along it:

  * reconciliation.run() writes a ReconciliationFinding row for whatever
    collect() returned -- it never looks at check_code;
  * open_findings() selects every unresolved row for the venue, with no
    filter on check_code;
  * digest._item_lines groups by check_code and DERIVES the heading from
    the code itself (`check_code.replace("_", " ").title()`), so there is
    no label table for a new check to be missing from. That matters: the
    same lookup-table shape was a live 500 in beo_proposals.review_rows,
    fixed the same day.

So a check registered in collect() reaches the digest BY CONSTRUCTION, and
no per-check test is needed or would prove anything extra. The thing that
can actually break is a check that is never registered -- which is why
each new check file carries its own AST test that collect() calls it.

THE END-TO-END HALF. A construction argument is worth exactly as much as
the one time somebody drove it. So the second test takes a real booking
that trips a real check, runs the real reconciliation, builds the real
digest, and reads the rendered body. If any link in that chain grows a
per-check condition, this fails.
"""
import ast
import datetime as dt
import inspect

from app.models.booking import BookingStatus
from app.models.invoice import InvoiceType
from app.services import digest, invoicing, reconciliation
from app.services.booking import change_status, create_booking

DASH = "https://book.meantime.com.au"


def _booking_short_of_its_minimum(db, loft, contact, name):
    """Trips FINAL_INVOICE_BELOW_MINIMUM_SPEND: agreed $1,500, invoiced
    $1,000. Chosen because it is one of the checks added on the day the
    rule was stated, so the proof covers the newest thing rather than the
    oldest."""
    from decimal import Decimal

    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    b.agreed_min_food_spend = Decimal("1500.00")
    db.flush()
    change_status(db, b, BookingStatus.confirmed, actor="test")
    invoice = invoicing.create_invoice(
        db, b, InvoiceType.final,
        [{"description": "Food", "quantity": 1, "unit_price": "1000.00"}],
        dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, invoice, actor="staff:test@meantime.com.au")
    db.flush()
    return b


# --- the structural half -----------------------------------------------------


def test_nothing_on_the_path_from_a_check_to_the_digest_looks_at_a_check_code():
    """The property that makes every check reach the email, asserted on the
    source of the three functions in the chain.

    A filter, a whitelist or a label lookup keyed on check_code anywhere
    here would mean a new check silently never arrives -- which is the one
    failure Aaron's rule is about, and it would not show up in any
    per-check test.
    """
    def _names_subscripted(fn):
        """Every `something[...]` and `something.get(...)` target in fn."""
        tree = ast.parse(inspect.getsource(fn).lstrip())
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                out.add(node.value.id)
        return out

    # open_findings must not narrow by code.
    source = inspect.getsource(reconciliation.open_findings)
    assert "check_code" not in source, (
        "open_findings filters on check_code, so a new check can be silently excluded"
    )

    # run() writes whatever collect() returned, without inspecting the code.
    run_source = inspect.getsource(reconciliation.run)
    assert "check_code ==" not in run_source and "check_code in" not in run_source, (
        "run() branches on check_code, so a new check may never be written"
    )

    # The digest derives its heading from the code rather than looking it up.
    item_source = inspect.getsource(digest._item_lines)
    assert 'check_code.replace("_", " ").title()' in item_source, (
        "the digest no longer derives a check's heading from its code -- if it now "
        "looks one up, a new check renders blank or raises"
    )


def test_every_check_collect_calls_is_a_real_function():
    """The other end of the same question. A check that exists and is never
    called is a safeguard that is really a log; a name in collect() that is
    not a function is a NameError at 20:30 with no digest at all."""
    tree = ast.parse(inspect.getsource(reconciliation.collect))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id.startswith("check_")
    }
    assert len(called) >= 15, f"collect() calls only {len(called)} checks -- did one get dropped?"
    for name in sorted(called):
        assert callable(getattr(reconciliation, name, None)), f"collect() calls {name}, which is not a function"


# --- the end-to-end half -----------------------------------------------------


def test_a_check_that_fires_tonight_is_in_the_digest_body(db, hamilton, loft, contact):
    """THE one. Real booking, real reconciliation run, real digest, real
    rendered text -- no per-check wiring anywhere, and this is what says
    so."""
    booking = _booking_short_of_its_minimum(db, loft, contact, "ZZDIGEST Reaches")

    reconciliation.run(db, hamilton)
    content = digest.build_digest(db, hamilton)
    subject, body = digest.render_digest_text(content, dashboard_base_url=DASH)

    assert "RECONCILIATION" in body, "the digest has no reconciliation section at all"
    assert "Final Invoice Below Minimum Spend" in body, (
        "the check fired and its heading is not in the digest -- derived from the code, "
        f"so this means the grouping changed. Body:\n{body}"
    )
    assert "ZZDIGEST Reaches" in body, "the booking the check fired on is not named"
    assert "$500.00 short" in body, "the finding's own detail did not reach the email"
    assert str(booking.id) in body, "no link to the booking"


def test_the_subject_counts_it(db, hamilton, loft, contact):
    """An item in the body that the subject does not count is how a subject
    comes to say 'all clear' over a body listing a problem."""
    _booking_short_of_its_minimum(db, loft, contact, "ZZDIGEST Counted")

    reconciliation.run(db, hamilton)
    content = digest.build_digest(db, hamilton)
    subject, _body = digest.render_digest_text(content, dashboard_base_url=DASH)

    assert content.item_count >= 1
    assert "all clear" not in subject.lower(), f"the subject says all clear over an open finding: {subject}"


def test_a_resolved_finding_leaves_the_digest_by_itself(db, hamilton, loft, contact):
    """Self-clearing, which is what makes the digest safe to trust: fix the
    shortfall and tomorrow's email simply does not mention it, with nothing
    to dismiss and nothing to miss if a run is skipped.

    ASSERTED ON THE CHECK, NOT THE BOOKING. The first version asserted the
    booking's name had left the digest entirely and failed -- correctly:
    this fixture confirms a booking without an agreement or a deposit, so
    CONFIRMED_WITHOUT_GATES fires on it too and keeps the name in the
    email. That is the digest working. What must clear is the finding that
    was resolved, and nothing else.
    """
    booking = _booking_short_of_its_minimum(db, loft, contact, "ZZDIGEST Clears")
    reconciliation.run(db, hamilton)
    assert "Final Invoice Below Minimum Spend" in digest.render_digest_text(
        digest.build_digest(db, hamilton), dashboard_base_url=DASH
    )[1]

    # Lower the agreed minimum to what was actually invoiced -- the
    # shortfall is gone, deliberately, which is one of the two fixes the
    # finding's own text suggests.
    from decimal import Decimal

    booking.agreed_min_food_spend = Decimal("1000.00")
    db.flush()

    reconciliation.run(db, hamilton)
    _subject, body = digest.render_digest_text(digest.build_digest(db, hamilton), dashboard_base_url=DASH)

    assert "Final Invoice Below Minimum Spend" not in body, "a resolved finding is still in the digest"
    assert "$500.00 short" not in body
    assert "ZZDIGEST Clears" in body, (
        "the booking vanished entirely -- CONFIRMED_WITHOUT_GATES still applies to it, "
        "so this would mean resolving one finding wrongly resolved another"
    )
