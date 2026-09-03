"""The client's words, staff notes, and what actually reaches a client.

Until this build all three were one field. booking.notes defaulted into the
client-facing Special Notes of a generated Event Order, so anything typed
there published itself the first time a BEO was made. These tests pin the
boundary in the direction that matters: staff text must not reach a client.
"""

import datetime as dt

import pytest

from app.models import Booking, Contact
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

PRIVATE = "Client haggled hard on price, do not offer the Loft again at that rate."


def _booking(db, loft, *, notes=None, enquiry_text=None):
    contact = Contact(name="Split Client", email="split.client@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=40),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_name="Split Test", event_type="corporate",
        adult_count=80, child_count=0, notes=notes, actor="staff:test",
        enquiry_text=enquiry_text,
    )


# --- the leak ------------------------------------------------------------


def test_staff_notes_never_reach_the_client_facing_section(db, loft):
    b = _booking(db, loft, notes=PRIVATE)
    content = generate_beo_content(b)
    assert PRIVATE not in content["special_notes"]
    assert content["special_notes"] == ""


def test_staff_notes_go_to_the_internal_section_instead(db, loft):
    """The information is kept, it just moves to the correct side."""
    b = _booking(db, loft, notes=PRIVATE)
    content = generate_beo_content(b)
    assert content["internal_notes"] == PRIVATE


def test_the_clients_own_words_do_not_publish_either(db, loft):
    b = _booking(db, loft, enquiry_text="Please make it a surprise, do not mention the cost to my husband.")
    content = generate_beo_content(b)
    assert "surprise" not in (content["special_notes"] or "")


def test_special_notes_appear_only_when_staff_type_them(db, loft):
    b = _booking(db, loft, notes=PRIVATE)
    content = generate_beo_content(b, special_notes_extra="Cake arriving 5pm, fridge space needed.")
    assert content["special_notes"] == "Cake arriving 5pm, fridge space needed."
    assert PRIVATE not in content["special_notes"]


def test_explicit_internal_notes_still_win_over_the_booking_default(db, loft):
    b = _booking(db, loft, notes=PRIVATE)
    content = generate_beo_content(b, internal_notes="Kitchen: double the arancini.")
    assert content["internal_notes"] == "Kitchen: double the arancini."


# --- the untrusted boundary ---------------------------------------------


def test_client_text_and_staff_text_are_different_fields(db, loft):
    b = _booking(db, loft, notes="Staff: chase the deposit", enquiry_text="Hi, we are after a Friday")
    db.refresh(b)
    assert b.enquiry_text == "Hi, we are after a Friday"
    assert b.notes == "Staff: chase the deposit"
    assert b.enquiry_text != b.notes


def test_the_enquiry_form_puts_the_clients_words_in_enquiry_text(db, hamilton):
    from app.services.enquiry_classification import create_enquiry_booking

    booking, _duplicates, _created = create_enquiry_booking(
        db, venue=hamilton, full_name="Jo Client", email="jo.client@example.com",
        phone=None, event_name="Jo's 40th", event_type="birthday",
        event_date=dt.date.today() + dt.timedelta(days=90),
        proposed_time_slot="evening", attendee_count=80, adult_count=80,
        company_name="Acme Pty Ltd", dates_flexible=True,
        comments="We would love the Loft if it is free.",
        lead_source=None, lead_referrer=None, actor="client (form)",
        first_touch_attribution=None, last_touch_attribution=None,
    )
    db.refresh(booking)
    assert booking.enquiry_text == "We would love the Loft if it is free."
    # The structured answers stay in notes; the client's prose does not.
    assert "Acme Pty Ltd" in booking.notes
    assert "We would love the Loft" not in (booking.notes or "")


def test_the_gate_still_reads_risk_signals_from_the_clients_own_words(db, hamilton, loft):
    """Splitting the field must not blind the gate to what a client wrote."""
    from app.services import draft_gate

    b = _booking(db, loft, enquiry_text="One of our guests has a severe nut allergy.")
    decision = draft_gate.evaluate(db, b, adult_count=80, attendee_count=80)
    assert draft_gate.DIETARY in decision.codes


# --- the reconciliation check -------------------------------------------


def test_notes_without_an_event_order_are_surfaced_for_review(db, hamilton, loft):
    from app.services import reconciliation

    _booking(db, loft, notes=PRIVATE)
    codes = {f.check_code for f in reconciliation.collect(db, hamilton)}
    assert "NOTES_BEFORE_BEO" in codes


def test_the_review_flag_clears_once_the_event_order_exists(db, hamilton, loft):
    from app.models.document import DocumentType
    from app.services import documents as documents_service
    from app.services import reconciliation

    b = _booking(db, loft, notes=PRIVATE)
    assert "NOTES_BEFORE_BEO" in {f.check_code for f in reconciliation.collect(db, hamilton)}

    documents_service.create_new_version(db, b, DocumentType.beo, {"x": 1}, actor="staff:test")
    assert "NOTES_BEFORE_BEO" not in {f.check_code for f in reconciliation.collect(db, hamilton)}


def test_a_booking_with_no_free_text_is_not_flagged(db, hamilton, loft):
    from app.services import reconciliation

    _booking(db, loft)
    assert "NOTES_BEFORE_BEO" not in {f.check_code for f in reconciliation.collect(db, hamilton)}
