"""Moving a booking to a new room does not rewrite a figure a client holds.

assign_space_and_time reconciles agreed_min_adults and
agreed_min_food_spend when they are still sitting at the OLD room's
default with no reason recorded -- the placeholder Unassigned space seeds
both to zero, and leaving zero behind on triage put "$0.00" in a client's
agreement header (2026-09-05). Right, and kept.

What it never asked is whether an AGREEMENT IS ALREADY OUT. An agreement
freezes both figures at generation, because a signed contract reflects
what was agreed. Move the room after it has been sent and the booking's
figure silently becomes the new room's while the contract the client is
reading -- or has signed -- still says the old one. Two figures for one
contractual term again, the shape this codebase has already paid for once.

So with a sent, viewed or signed agreement current, neither figure moves.
The move still happens; the booking is flagged for review, because the
contract needs re-issuing at the new room's terms and that is a person's
call. A draft agreement, or none, and the reconciliation runs as before.

EVERY PROBE MOVES FROM THE PLACEHOLDER with the agreement OUT, because with
no agreement the old behaviour is the right one and would pass.
"""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.models import BookingEvent
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import assign_space_and_time, create_booking
from app.services.document_generation import generate_agreement_content


def _placeholder_booking(db, unassigned_space, contact, name):
    b = create_booking(
        db, space_id=unassigned_space.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=40), start_time=None, end_time=None,
        event_name=name, event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()
    assert b.agreed_min_food_spend == Decimal("0.00"), "the placeholder seeds zero"
    return b


def _agreement(db, booking, *, send=True, sign=False):
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    db.flush()
    if send:
        documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
        db.flush()
    if sign:
        documents_service.sign(db, doc, signer_name="Test Client", signer_ip="127.0.0.1")
        db.flush()
    return doc


def _move(db, booking, loft):
    assign_space_and_time(
        db, booking, space_id=loft.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0), actor="staff:test"
    )
    db.refresh(booking)


def _flags(db, booking):
    return db.scalars(
        select(BookingEvent.new_value).where(
            BookingEvent.booking_id == booking.id,
            BookingEvent.event_type == "enquiry_flagged",
            BookingEvent.field_name == "manual_review",
        )
    ).all()


def test_a_sent_agreement_holds_the_food_minimum_and_the_booking_is_flagged(
    db, hamilton, unassigned_space, loft, contact
):
    """THE one. The client is reading a contract that says $0 minimum."""
    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE Sent")
    _agreement(db, booking)

    _move(db, booking, loft)

    assert booking.space_id == loft.id, "the move itself must still happen"
    assert booking.agreed_min_food_spend == Decimal("0.00"), (
        "the booking's minimum moved while the sent agreement still names the old one"
    )
    assert any("agreement" in (note or "") for note in _flags(db, booking)), "nobody was told"


def test_a_sent_agreement_holds_the_guest_minimum_too(db, hamilton, unassigned_space, loft, contact):
    """Same guard, the other figure -- swept as a class, not an instance."""
    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE Guests")
    assert booking.agreed_min_adults == 0
    _agreement(db, booking)

    _move(db, booking, loft)

    assert booking.agreed_min_adults == 0
    assert loft.standard_min_adults != 0, "fixture: the new room must have a different standard"


def test_a_signed_agreement_holds_both(db, hamilton, unassigned_space, loft, contact):
    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE Signed")
    _agreement(db, booking, sign=True)

    _move(db, booking, loft)

    assert booking.agreed_min_food_spend == Decimal("0.00")
    assert booking.agreed_min_adults == 0
    assert _flags(db, booking)


def test_with_no_agreement_the_figures_follow_the_room(db, hamilton, unassigned_space, loft, contact):
    """The positive control, and the 2026-09-05 fix: triage into a real
    room carries the room's minimums when nothing has been quoted."""
    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE None")

    _move(db, booking, loft)

    assert booking.agreed_min_food_spend == loft.min_food_spend
    assert booking.agreed_min_adults == loft.standard_min_adults
    assert _flags(db, booking) == []


def test_a_draft_agreement_does_not_hold_them(db, hamilton, unassigned_space, loft, contact):
    """A draft was never shown to a client; there is nothing they hold."""
    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE Draft")
    _agreement(db, booking, send=False)

    _move(db, booking, loft)

    assert booking.agreed_min_food_spend == loft.min_food_spend
    assert _flags(db, booking) == []


def test_a_deliberately_set_figure_is_still_left_alone(db, hamilton, unassigned_space, loft, contact):
    """Unchanged behaviour: a figure somebody set with a reason never moves,
    agreement or not."""
    from app.models.booking import MinReductionReasonCode
    from app.services.booking import set_agreed_food_minimum

    booking = _placeholder_booking(db, unassigned_space, contact, "ZZMOVE Deliberate")
    set_agreed_food_minimum(
        db, booking, agreed_min_food_spend=Decimal("300.00"),
        reason=MinReductionReasonCode.friday_incentive, actor="staff:test",
    )

    _move(db, booking, loft)

    assert booking.agreed_min_food_spend == Decimal("300.00")
