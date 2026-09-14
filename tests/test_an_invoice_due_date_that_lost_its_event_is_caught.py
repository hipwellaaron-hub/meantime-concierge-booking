"""A sent invoice whose due date no longer relates to its event.

From the stale-copy register. invoices.due_date is set once, from the
event date, and never re-derived -- while the invoice's own "Date of
Service" column reads booking.event_date LIVE. Postpone an event and the
two disagree on the client's own copy: a balance due in March for a
service in June.

REPORTED, NEVER MOVED, for the reason that runs through the whole
register: a due date on an issued invoice is a term the client was given,
and quietly changing what they owe and when is Revise's job, with a person
behind it.

EVERY PROBE MOVES THE EVENT AFTER THE INVOICE WENT OUT. An invoice created
today for an event next month has a perfectly ordinary lead time, so a
check that compared nothing would pass on it.
"""
import datetime as dt

from app.models.invoice import InvoiceType
from app.services import invoicing, policy, reconciliation
from app.services.booking import create_booking

TODAY = dt.date.today()


def _booking(db, loft, contact, name, *, event_in_days=30):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=TODAY + dt.timedelta(days=event_in_days), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _sent(db, booking, *, due, kind=InvoiceType.final, send=True):
    inv = invoicing.create_invoice(
        db, booking, kind, [{"description": "Food", "quantity": 1, "unit_price": "1000.00"}],
        due, actor="test",
    )
    db.flush()
    if send:
        invoicing.mark_sent(db, inv, actor="staff:test@meantime.com.au")
        db.flush()
    return inv


def _codes(findings):
    return [f.check_code for f in findings]


def test_a_postponed_event_leaves_its_invoice_due_date_behind(db, hamilton, loft, contact):
    """THE one. Invoice issued for an event next month, due a week before
    it; the event then moves six months out."""
    booking = _booking(db, loft, contact, "ZZDUE Postponed")
    invoice = _sent(db, booking, due=TODAY + dt.timedelta(days=23))

    booking.event_date = TODAY + dt.timedelta(days=200)
    db.flush()

    findings = reconciliation.check_invoice_due_date_predates_the_event(db, [booking])

    assert _codes(findings) == ["INVOICE_DUE_DATE_STALE"]
    assert invoice.invoice_reference in findings[0].detail
    assert invoice.due_date == TODAY + dt.timedelta(days=23), "the check moved a client's due date"


def test_an_ordinary_lead_time_is_silent(db, hamilton, loft, contact):
    """A balance falls due seven days before the event, a deposit on
    issue. Both are well inside the tolerance and must stay quiet."""
    booking = _booking(db, loft, contact, "ZZDUE Ordinary")
    _sent(db, booking, due=booking.event_date - dt.timedelta(days=7))

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [booking]) == []


def test_a_deposit_due_on_issue_is_silent(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZDUE Deposit", event_in_days=25)
    _sent(db, booking, due=TODAY, kind=InvoiceType.deposit)

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [booking]) == []


def test_a_due_date_after_the_event_is_not_a_finding(db, hamilton, loft, contact):
    """The ordinary shape for a balance settled afterwards. This check is
    about a due date that has fallen BEHIND a moved event, not about one
    that sits late."""
    booking = _booking(db, loft, contact, "ZZDUE After")
    _sent(db, booking, due=booking.event_date + dt.timedelta(days=14))

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [booking]) == []


def test_a_draft_is_not_reported(db, hamilton, loft, contact):
    """A draft re-derives on its next edit and is not the client's yet."""
    booking = _booking(db, loft, contact, "ZZDUE Draft")
    _sent(db, booking, due=TODAY + dt.timedelta(days=23), send=False)
    booking.event_date = TODAY + dt.timedelta(days=200)
    db.flush()

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [booking]) == []


def test_a_booking_with_no_event_date_is_not_an_error(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZDUE NoDate")
    _sent(db, booking, due=TODAY + dt.timedelta(days=10))
    booking.event_date = None
    db.flush()

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [booking]) == []


def test_the_boundary_is_the_policy_constant(db, hamilton, loft, contact):
    """One day inside the tolerance is silent; one day outside is not. A
    check whose threshold nothing pins is a number that drifts."""
    lead = policy.INVOICE_DUE_DATE_MAX_LEAD_DAYS
    quiet = _booking(db, loft, contact, "ZZDUE Inside")
    _sent(db, quiet, due=quiet.event_date - dt.timedelta(days=lead))
    loud = _booking(db, loft, contact, "ZZDUE Outside")
    _sent(db, loud, due=loud.event_date - dt.timedelta(days=lead + 1))

    assert reconciliation.check_invoice_due_date_predates_the_event(db, [quiet]) == []
    assert _codes(reconciliation.check_invoice_due_date_predates_the_event(db, [loud])) == [
        "INVOICE_DUE_DATE_STALE"
    ]


def test_the_check_is_wired_into_collect():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(reconciliation.collect))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "check_invoice_due_date_predates_the_event" in called
