"""The audit trail says WHICH fields a hand-edit changed.

It recorded that a version was edited and nothing about what was in it. On
2026-09-08 that turned a simple question -- did that edit touch the food
order? -- into arithmetic against an invoice cut before the edit, because
the log could not answer it at all. Aaron: "the whole reason I had to do
arithmetic on invoice totals today is that the log doesn't say what
changed."

The names go in `old_value`, which `document_edited` leaves empty. They
cannot go in `field_name` or `new_value`: document_regeneration's
was_hand_edited and _last_hand_edit_at both match on exactly those two, and
so did the production audit that diagnosed the incident.
"""

import datetime as dt

import pytest

from app.models import BookingEvent, Contact
from app.models.document import DocumentType
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content


def _booking(db, space, name="Edit Audit"):
    contact = Contact(name="Edit Audit", email=f"edit.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


def _edit_event(db, booking):
    events = [
        event
        for event in db.query(BookingEvent).filter_by(
            booking_id=booking.id, event_type="document_edited"
        ).all()
    ]
    assert len(events) == 1, [e.old_value for e in events]
    return events[0]


def test_an_edit_names_the_field_it_changed(db, loft):
    booking = _booking(db, loft)
    document = _beo(db, booking)

    documents_service.update_content(
        db, document, {**document.content, "dietaries": "1x severe nut allergy (table 4)."},
        actor="staff:aaron",
    )

    assert _edit_event(db, booking).old_value == "dietaries"


def test_it_names_every_field_that_moved(db, loft):
    booking = _booking(db, loft, "Edit Many")
    document = _beo(db, booking)

    documents_service.update_content(
        db, document,
        {
            **document.content,
            "dietaries": "1x nut allergy.",
            "room_layout_notes": "Rounds of 8.",
            "onsite_contact": "Sally 0400 000 000",
        },
        actor="staff:aaron",
    )

    assert _edit_event(db, booking).old_value == "dietaries, onsite_contact, room_layout_notes"


def test_the_food_order_is_named_when_it_moves(db, loft):
    """The question that could not be answered on 2026-09-08."""
    booking = _booking(db, loft, "Edit Food")
    document = _beo(db, booking)

    documents_service.update_content(
        db, document,
        {
            **document.content,
            "food_order": {
                "line_items": [{"description": "Oyster station", "quantity": 4, "unit_price": "180.00"}],
                "note": None,
            },
        },
        actor="staff:aaron",
    )

    assert "food_order" in _edit_event(db, booking).old_value


def test_a_save_that_changes_nothing_names_nothing(db, loft):
    """An empty list would read as "we do not know". None is "nothing
    moved", which is a different and true answer."""
    booking = _booking(db, loft, "Edit Nothing")
    document = _beo(db, booking)

    documents_service.update_content(db, document, dict(document.content), actor="staff:aaron")

    assert _edit_event(db, booking).old_value is None


def test_the_authorship_record_is_not_listed(db, loft):
    """_authored is rewritten by nearly every save. Listing it would put
    noise on every line of the trail."""
    booking = _booking(db, loft, "Edit Record")
    document = _beo(db, booking)

    documents_service.update_content(
        db, document, {**document.content, "dietaries": "1x nut allergy."},
        actor="staff:aaron",
        authored_fields=dr.PROTECTED_FIELD_NAMES,
        placeholders=dr.GENERATED_PLACEHOLDERS,
    )

    db.refresh(document)
    assert "dietaries" in document.content["_authored"], "the record was written, so it did change"
    assert "_authored" not in (_edit_event(db, booking).old_value or "")


def test_replacing_real_text_with_a_placeholder_is_still_recorded(db, loft):
    """differing_fields, not changed_fields: a save that wipes somebody's
    words back to the generator's sentence is exactly the kind of edit the
    trail has to name."""
    booking = _booking(db, loft, "Edit Wipe")
    content = generate_beo_content(booking)
    content["room_layout_notes"] = "Rounds of 8, dance floor centre."
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )

    documents_service.update_content(
        db, document,
        {**document.content, "room_layout_notes": "[REVIEW] add room layout notes"},
        actor="staff:aaron",
        authored_fields=dr.PROTECTED_FIELD_NAMES,
        placeholders=dr.GENERATED_PLACEHOLDERS,
    )

    assert "room_layout_notes" in _edit_event(db, booking).old_value


# --- the readers that must not move -------------------------------------------


def test_the_hand_edit_readers_still_work(db, loft):
    """was_hand_edited and _last_hand_edit_at match on field_name and
    new_value. Putting the field list anywhere near those would break the
    approval badge silently."""
    booking = _booking(db, loft, "Edit Readers")
    document = _beo(db, booking)

    documents_service.update_content(
        db, document, {**document.content, "dietaries": "1x nut allergy."}, actor="staff:aaron"
    )
    db.refresh(document)

    found = dr.was_hand_edited(db, document)
    assert found is not None, "the regenerate screen stopped seeing the hand-edit"
    assert found.new_value == str(document.version)
    assert found.field_name == "beo_version"


def test_a_merge_records_only_the_keys_it_merged(db, loft):
    """update_content_fields writes specific keys; the ones it never touched
    are not part of what it changed."""
    booking = _booking(db, loft, "Edit Merge")
    document = _beo(db, booking)

    documents_service.update_content_fields(
        db, document, {"dietaries": "1x nut allergy."}, actor="staff:aaron"
    )

    assert _edit_event(db, booking).old_value == "dietaries"


def test_the_authorship_record_can_never_appear_in_the_list():
    """Not because this module filters for it -- it does not. Every name
    goes through content_authorship._names_to_write, which refuses
    underscore keys, so the guarantee lives there and is tested there. This
    pins the consequence at this level so a future rewrite that stops
    routing through differing_fields cannot quietly start listing it."""
    changed = documents_service._fields_this_save_changed(
        {"_authored": ["dietaries"], "dietaries": "old"},
        {"_authored": ["dietaries", "music"], "dietaries": "new"},
    )

    assert changed == "dietaries"
