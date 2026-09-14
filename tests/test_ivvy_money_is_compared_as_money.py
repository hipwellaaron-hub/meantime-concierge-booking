"""The iVvy reconciliation compares money as numbers, against live records.

This module's stated purpose is "do not cancel iVvy until this report runs
clean". Three faults sat under that sentence, and all three failed in the
direction of saying everything was fine.

  * IT COMPARED STRINGS. parse_money returned str(Decimal(v)), which keeps
    whatever formatting its source used -- so '500' != '500.00' reported a
    divergence between two identical amounts. The August import stored 2dp
    strings while a raw Bookings export carries unpadded ones, so a re-run
    could diverge on every row for no reason at all.

  * AN UNREADABLE FIGURE BECAME None, AND None MATCHED None. '$500.00' on
    both sides parsed to None on both sides and counted as MATCHED CLEAN.

  * IT COMPARED iVvy AGAINST migration_snapshot -- iVvy's own figure,
    frozen at import. That can only ever detect iVvy changing since the
    import. It could never see Concierge's live money drifting from iVvy's,
    which is the single thing a parallel run exists to catch.
"""
import datetime as dt
from decimal import Decimal

from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services.ivvy_import import import_ivvy_csv
from app.services.ivvy_reconciliation import _live_money, _money_or_none, reconcile
from tests.test_ivvy_import import _row, _write_csv


# --- reading a figure -------------------------------------------------------


def test_the_same_amount_written_two_ways_is_the_same_amount():
    """'500' and '500.00' are one number. As strings they were a
    divergence, and a re-run could have reported one on every row."""
    assert _money_or_none("500") == _money_or_none("500.00") == Decimal("500")


def test_the_shapes_an_export_actually_carries_are_readable():
    """A column reading '$1,450.00' is a readable figure. Calling it
    unreadable would report a divergence on a row that agrees."""
    assert _money_or_none("$1,450.00") == Decimal("1450.00")
    assert _money_or_none("(250.00)") == Decimal("-250.00")
    assert _money_or_none("  500  ") == Decimal("500")


def test_genuinely_unreadable_is_none_and_blank_is_none():
    assert _money_or_none("not a number") is None
    assert _money_or_none("") is None
    assert _money_or_none(None) is None


# --- against LIVE records ---------------------------------------------------


def _booking_with(db, hamilton, tmp_path, *, paid, total):
    csv_path = _write_csv(tmp_path, [_row(code="M1", paid="0", outstanding="0")])
    import_ivvy_csv(db, csv_path, venue=hamilton)
    from sqlalchemy import select

    from app.models import Booking

    booking = db.scalars(select(Booking).where(Booking.migration_external_ref == "M1")).first()
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.final,
        [{"description": "Balance", "quantity": 1, "unit_price": str(total)}],
        dt.date.today() + dt.timedelta(days=30), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, invoice, actor="staff:test@meantime.com.au")
    db.flush()
    if Decimal(paid) > 0:
        invoicing.record_payment(
            db, invoice, amount=Decimal(paid), method=PaymentMethod.bank_transfer, actor="test"
        )
        db.flush()
    return booking


def test_live_money_reads_the_payments_table(db, hamilton, tmp_path):
    booking = _booking_with(db, hamilton, tmp_path, paid="500", total="1450")

    paid, outstanding = _live_money(db, booking)

    assert paid == Decimal("500.00")
    assert outstanding == Decimal("950.00")


def test_concierge_money_drifting_from_ivvy_is_caught(db, hamilton, tmp_path):
    """THE one the old check could never see. iVvy says $1,450 was paid;
    Concierge has recorded $500. The snapshot comparison compared iVvy's
    frozen figure with iVvy's current one and called it clean."""
    _booking_with(db, hamilton, tmp_path, paid="500", total="1450")

    export = _write_csv(
        tmp_path, [_row(code="M1", paid="1450", outstanding="0")], filename="live.csv"
    )
    report = reconcile(db, export, venue=hamilton)

    assert report.is_clean is False
    paid_divergence = next(d for d in report.divergences if d.field == "total_paid")
    assert paid_divergence.concierge_value == "500.00"
    assert paid_divergence.ivvy_value == "1450"


def test_agreeing_money_written_differently_is_clean(db, hamilton, tmp_path):
    """The false-divergence half: Concierge holds 500.00, iVvy writes 500."""
    _booking_with(db, hamilton, tmp_path, paid="500", total="500")

    export = _write_csv(
        tmp_path, [_row(code="M1", paid="500", outstanding="0")], filename="agree.csv"
    )
    report = reconcile(db, export, venue=hamilton)

    money = [d for d in report.divergences if d.field in ("total_paid", "total_outstanding")]
    assert money == [], f"identical amounts reported as divergent: {money}"


def test_an_unreadable_figure_is_a_divergence_not_a_match(db, hamilton, tmp_path):
    """It used to parse to None on both sides and count as matched clean --
    silence, in the direction of saying everything is fine."""
    _booking_with(db, hamilton, tmp_path, paid="500", total="500")

    export = _write_csv(
        tmp_path, [_row(code="M1", paid="about five hundred", outstanding="0")], filename="bad.csv"
    )
    report = reconcile(db, export, venue=hamilton)

    assert report.is_clean is False
    d = next(d for d in report.divergences if d.field == "total_paid")
    assert "unreadable" in d.ivvy_value
