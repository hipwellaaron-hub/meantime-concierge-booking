"""A final invoice's balance falls due before the event, not on it.

It used to be dated `booking.event_date`, so a client had nothing to pay
against until the day they walked in. Nothing anywhere stated what it
should be -- not policy.py, not the agreement's terms -- so the figure came
from Aaron directly on 2026-09-08: seven days before the event, due on
issue if that has already passed.

The floor is the part that needs a test rather than a constant. The wizard
routinely comes back inside seven days: the two bookings live when this was
written had theirs submitted 3 and 5 days out, so a bare event_date - 7
would have created an invoice already overdue on the day it was issued and
dropped a client who had done nothing wrong straight into arrears chasing.
"""

import datetime as dt

import pytest

from app.services import policy

EVENT = dt.date(2027, 5, 14)


def test_the_ordinary_case_is_seven_days_before():
    assert policy.final_balance_due_date(EVENT, issued_on=dt.date(2027, 1, 1)) == dt.date(2027, 5, 7)


def test_the_constant_is_the_one_that_is_read():
    """Not a literal at the call site -- that is how the Event Order lead
    time ended up with three different figures in four places."""
    assert policy.FINAL_BALANCE_DUE_DAYS_BEFORE_EVENT == 7
    assert policy.final_balance_due_date(EVENT, issued_on=dt.date(2027, 1, 1)) == EVENT - dt.timedelta(
        days=policy.FINAL_BALANCE_DUE_DAYS_BEFORE_EVENT
    )


@pytest.mark.parametrize(
    "event, issued",
    [
        (dt.date(2026, 9, 11), dt.date(2026, 9, 8)),   # HAM-20260911-AKPSO, 3 days out
        (dt.date(2026, 9, 12), dt.date(2026, 9, 7)),   # HAM-20260912-2R11Q, 5 days out
    ],
)
def test_a_late_wizard_is_never_issued_already_overdue(event, issued):
    """Both live bookings. event_date - 7 is in the past for each."""
    due = policy.final_balance_due_date(event, issued_on=issued)

    assert due == issued, "a client would have been in arrears the moment the invoice existed"
    assert due >= issued


def test_it_is_never_dated_before_issue():
    assert policy.final_balance_due_date(dt.date(2026, 9, 1), issued_on=dt.date(2026, 9, 8)) == dt.date(
        2026, 9, 8
    )


def test_exactly_seven_days_out_is_due_that_day():
    """The boundary: the floor must not push it forward a day."""
    issued = EVENT - dt.timedelta(days=7)
    assert policy.final_balance_due_date(EVENT, issued_on=issued) == issued


def test_eight_days_out_is_still_seven_days_before():
    assert policy.final_balance_due_date(
        EVENT, issued_on=EVENT - dt.timedelta(days=8)
    ) == EVENT - dt.timedelta(days=7)


# --- through the wizard, which is what actually dates these --------------------


def test_the_wizard_dates_the_final_invoice_by_the_rule(db, loft, menu_items, monkeypatch):
    """The call site, not just the helper. Before this it passed
    booking.event_date straight through."""
    import datetime as _dt

    from app.models.booking import BookingStatus
    from app.models.invoice import InvoiceType
    from app.services import wizard as wizard_service
    from app.services.booking import change_status
    from tests.test_wizard_generation import _complete_all_steps, _make_booking, _pay_deposit

    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)

    _, result = wizard_service.submit_review(db, session, actor="test")

    assert result.invoice.type == InvoiceType.final
    expected = policy.final_balance_due_date(booking.event_date, issued_on=_dt.date.today())
    assert result.invoice.due_date == expected
    assert result.invoice.due_date != booking.event_date, "still dated on the event"


def test_an_undated_booking_gets_no_suggestion():
    """An enquiry can reach the booking page before anybody has agreed a
    date. Inventing one would date an invoice from nothing -- and this is
    not hypothetical: it raised a TypeError on five existing tests the
    moment the prefill was added."""
    assert policy.final_balance_due_date(None, issued_on=dt.date(2026, 9, 8)) is None
