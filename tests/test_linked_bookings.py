"""A real event that needs two physical spaces at once (surfaced by the
iVvy reconciliation's "multi-space" cases) is modelled as a second real
Booking row linked to the first via parent_booking_id, not a pseudo Space.
These tests cover: creating a linked space, the guards that keep
documents/invoices/wizard on the parent only, and status cascading from
parent to still-active children.
"""

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.services import documents as documents_service
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services.booking import add_linked_space, change_status, create_booking, transition_status
from app.services.document_generation import generate_agreement_content


def _contact(db):
    contact = Contact(name="Linked Test Contact", email="linked.test@example.com")
    db.add(contact)
    db.flush()
    return contact


def _parent_booking(db, space, *, status=BookingStatus.enquiry, contact=None):
    return create_booking(
        db, space_id=space.id, contact_id=(contact or _contact(db)).id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Big Combined Event",
        event_type="wedding", adult_count=120, child_count=0, notes=None, actor="test", status=status,
    )


# --- creating a linked space -------------------------------------------------


def test_add_linked_space_creates_a_second_real_booking(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")

    assert child.id != parent.id
    assert child.space_id == mezzanine.id
    assert child.parent_booking_id == parent.id
    assert child.event_name == parent.event_name
    assert child.event_date == parent.event_date
    assert child.status == parent.status
    assert child.contact_id == parent.contact_id


def test_add_linked_space_mirrors_parent_times_by_default(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    assert child.start_time == parent.start_time
    assert child.end_time == parent.end_time


def test_add_linked_space_accepts_a_different_time_window(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(
        db, parent, space_id=mezzanine.id, start_time=dt.time(20, 0), end_time=dt.time(23, 30), actor="test"
    )
    assert child.start_time == dt.time(20, 0)
    assert child.end_time == dt.time(23, 30)


def test_add_linked_space_appears_in_parent_linked_bookings(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    db.refresh(parent)
    assert [c.id for c in parent.linked_bookings] == [child.id]


def test_cannot_link_the_same_space_the_parent_already_occupies(db, loft):
    parent = _parent_booking(db, loft)
    with pytest.raises(ValueError, match="already part of this booking"):
        add_linked_space(db, parent, space_id=loft.id, actor="test")


def test_cannot_link_the_same_space_twice(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    with pytest.raises(ValueError, match="already part of this booking"):
        add_linked_space(db, parent, space_id=mezzanine.id, actor="test")


def test_cannot_link_a_space_to_a_child_booking(db, loft, mezzanine, lounge):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    with pytest.raises(ValueError, match="itself a linked child"):
        add_linked_space(db, child, space_id=lounge.id, actor="test")


def test_linking_a_conflicting_space_raises_integrity_error(db, loft, mezzanine):
    contact = _contact(db)
    # Something else already holds the Mezzanine at this exact time.
    create_booking(
        db, space_id=mezzanine.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Unrelated Booking",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test", status=BookingStatus.confirmed,
    )
    parent = _parent_booking(db, loft, status=BookingStatus.confirmed, contact=contact)
    with pytest.raises(IntegrityError):
        add_linked_space(db, parent, space_id=mezzanine.id, actor="test", start_time=dt.time(18, 0), end_time=dt.time(23, 0))
    db.rollback()


# --- documents/invoices/wizard stay on the parent -----------------------------


def test_cannot_create_document_on_a_linked_child(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    with pytest.raises(ValueError, match="parent booking"):
        documents_service.create_new_version(db, child, DocumentType.agreement, generate_agreement_content(child), actor="test")


def test_cannot_create_invoice_on_a_linked_child(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    with pytest.raises(ValueError, match="parent booking"):
        invoicing.create_invoice(
            db, child, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
            dt.date.today(), actor="test",
        )


def test_cannot_send_wizard_link_for_a_linked_child(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    with pytest.raises(ValueError, match="parent booking"):
        wizard_service.get_or_create_session(db, child, actor="test")


def test_documents_and_invoices_work_normally_on_the_parent(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    # No exception -- the parent itself is unaffected by having children.
    document = documents_service.create_new_version(db, parent, DocumentType.agreement, generate_agreement_content(parent), actor="test")
    assert document.booking_id == parent.id


# --- status cascade ------------------------------------------------------------


def test_confirming_parent_cascades_to_active_children(db, loft, mezzanine):
    parent = _parent_booking(db, loft)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    change_status(db, parent, BookingStatus.offered, actor="test")
    change_status(db, child, BookingStatus.offered, actor="test")
    change_status(db, parent, BookingStatus.tentative, actor="test")
    change_status(db, child, BookingStatus.tentative, actor="test")

    transition_status(db, parent, BookingStatus.confirmed, actor="test")

    db.refresh(child)
    assert parent.status == BookingStatus.confirmed
    assert child.status == BookingStatus.confirmed


def test_cancelling_parent_cascades_to_active_children(db, loft, mezzanine):
    parent = _parent_booking(db, loft, status=BookingStatus.tentative)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")

    transition_status(db, parent, BookingStatus.cancelled, actor="test")

    db.refresh(child)
    assert child.status == BookingStatus.cancelled


def test_cascade_skips_a_child_already_ended_independently(db, loft, mezzanine):
    parent = _parent_booking(db, loft, status=BookingStatus.tentative)
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    # Staff released just this room early, independent of the main event.
    transition_status(db, child, BookingStatus.cancelled, actor="test")

    transition_status(db, parent, BookingStatus.confirmed, actor="test")

    db.refresh(child)
    assert parent.status == BookingStatus.confirmed
    assert child.status == BookingStatus.cancelled  # untouched by the parent's move


def test_transitioning_a_child_directly_does_not_affect_the_parent_or_siblings(db, loft, mezzanine, lounge):
    parent = _parent_booking(db, loft, status=BookingStatus.tentative)
    child_a = add_linked_space(db, parent, space_id=mezzanine.id, actor="test")
    child_b = add_linked_space(db, parent, space_id=lounge.id, actor="test")

    transition_status(db, child_a, BookingStatus.cancelled, actor="test")

    db.refresh(parent)
    db.refresh(child_b)
    assert parent.status == BookingStatus.tentative
    assert child_b.status == BookingStatus.tentative


def test_cascade_reaches_the_calendar_and_availability_consistently(db, hamilton, loft, mezzanine):
    from app.services import calendar as calendar_service
    from app.services.availability import is_space_free

    parent = _parent_booking(db, loft, status=BookingStatus.tentative)
    add_linked_space(db, parent, space_id=mezzanine.id, actor="test")

    date = parent.event_date
    assert is_space_free(db, loft.id, date)[0] is False
    assert is_space_free(db, mezzanine.id, date)[0] is False

    transition_status(db, parent, BookingStatus.cancelled, actor="test")

    assert is_space_free(db, loft.id, date)[0] is True
    assert is_space_free(db, mezzanine.id, date)[0] is True

    # Cancelled bookings are deliberately excluded from CALENDAR_STATUSES
    # (see app.services.calendar) -- a genuinely free room must not still
    # show a chip.
    grid = calendar_service.get_week_grid(db, hamilton, calendar_service.week_start_for(date))
    assert grid["cells"][loft.id][date] == []
    assert grid["cells"][mezzanine.id][date] == []
