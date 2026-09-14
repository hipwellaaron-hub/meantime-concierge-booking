"""The deposit invoice bills the figure the client's agreement froze.

From the stale-copy register: the agreement freezes deposit_required at
generation -- a signed contract reflects what was agreed -- while
create_deposit_invoice read policy.STANDARD_DEPOSIT live. Two halves of one
figure, one frozen and one not: the day the constant moves, a client
holding a contract that says $500 gets an invoice for something else. The
minimum-spend clause had exactly this shape on 2026-09-05.

EVERY PROBE FORCES THE DISAGREEMENT. With the agreement's figure equal to
the constant -- which is every real booking today -- an invoice that still
read the constant would print the right number and prove nothing.
"""
import datetime as dt
from decimal import Decimal

from app.models.document import DocumentType
from app.services import documents as documents_service, invoicing, policy
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _agreement(db, booking, deposit_required):
    content = generate_agreement_content(booking)
    if deposit_required is None:
        content.pop("deposit_required", None)
    else:
        content["deposit_required"] = deposit_required
    doc = documents_service.create_new_version(db, booking, DocumentType.agreement, content, actor="test")
    db.flush()
    return doc


def _deposit_total(db, booking):
    inv = invoicing.create_deposit_invoice(db, booking, due_date=dt.date.today() + dt.timedelta(days=7), actor="test")
    db.flush()
    return Decimal(str(inv.total))


def test_the_invoice_bills_the_agreements_frozen_figure(db, hamilton, loft, contact):
    """THE one. The contract says $600; the constant says $500; the client
    is billed what the contract says."""
    booking = _booking(db, loft, contact, "ZZDEPOSIT Frozen")
    _agreement(db, booking, "600.00")
    assert policy.STANDARD_DEPOSIT != Decimal("600.00"), "the probe must disagree with the constant"

    assert _deposit_total(db, booking) == Decimal("600.00")


def test_with_no_agreement_the_house_figure_stands(db, hamilton, loft, contact):
    """There is no contract to disagree with."""
    booking = _booking(db, loft, contact, "ZZDEPOSIT NoAgreement")

    assert _deposit_total(db, booking) == policy.STANDARD_DEPOSIT


def test_an_agreement_that_predates_the_key_falls_back(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZDEPOSIT OldShape")
    _agreement(db, booking, None)

    assert _deposit_total(db, booking) == policy.STANDARD_DEPOSIT


def test_an_unreadable_figure_falls_back_rather_than_raising(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, "ZZDEPOSIT Garbage")
    _agreement(db, booking, "five hundred")

    assert _deposit_total(db, booking) == policy.STANDARD_DEPOSIT


def test_only_the_current_agreement_is_consulted(db, hamilton, loft, contact):
    """A superseded version's figure is history. The current one is what
    the client is being asked to sign, or has signed."""
    booking = _booking(db, loft, contact, "ZZDEPOSIT Superseded")
    _agreement(db, booking, "700.00")
    _agreement(db, booking, "650.00")  # supersedes the first

    assert _deposit_total(db, booking) == Decimal("650.00")


def test_deposit_figure_for_is_what_the_invoice_uses(db, hamilton, loft, contact):
    """The helper and the invoice cannot disagree: one is the other."""
    booking = _booking(db, loft, contact, "ZZDEPOSIT Helper")
    _agreement(db, booking, "550.00")

    assert invoicing.deposit_figure_for(db, booking) == Decimal("550.00")
    assert _deposit_total(db, booking) == invoicing.deposit_figure_for(db, booking)
