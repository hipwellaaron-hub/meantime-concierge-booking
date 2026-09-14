"""A draft Event Order goes out at today's catalogue price -- unless a
person set the price.

From the stale-copy register: a food line's unit_price is frozen when the
item is selected. Right for a SENT Event Order (it is the client's quote),
wrong for a draft that sat while the catalogue moved -- it goes out at a
withdrawn figure and the invoice built from it bills that figure. Platters
have no legacy variant, so a platter price change reaches every booking
immediately. check_food_price_drift REPORTS this; nothing repaired it.

The repair is at mark_sent, the one moment a draft becomes a quote, and it
is gated on whether a PERSON wrote the food order. That is recorded, not
guessed, in two places -- content_authorship's positive set, and the
document_edited event stamped on the version -- because a freshly
generated draft has no authorship record at all and the module's own
docstring says the audit trail is the caller's to read. A price a person
typed is a commercial decision and stays; the nightly check keeps
reporting it. Where nobody typed anything, the catalogue was the only
author and the catalogue is what the client should be quoted.

EVERY PROBE FORCES THE STALE CONDITION AND THE ABSENCE OF A PERSON. A
draft built at today's price, or one a person touched, is left alone by
design -- so a helper that did nothing at all would pass those. The
assertions that matter start from a stale, unauthored line.
"""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.models import BookingEvent
from app.models.document import DocumentStatus, DocumentType
from app.services import beo_proposals, content_authorship, documents as documents_service
from app.services.booking import create_booking


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _a_platter(menu_items):
    from app.models.menu_item import MenuItemCategory

    return next(i for i in menu_items.values() if i.category == MenuItemCategory.platter)


def _draft(db, booking, lines):
    """A draft Event Order carrying exactly these food lines and NO
    authorship record -- the shape a fresh generation leaves behind."""
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {"line_items": lines, "note": None}
    content.pop(content_authorship.AUTHORED_KEY, None)
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    assert not content_authorship.has_record(doc.content), "fixture must start with no record"
    return doc


def _catalogue_line(item, quoted, **extra):
    return {
        "description": item.name, "quantity": 2, "unit_price": str(quoted),
        "category": item.category.value, "menu_item_id": str(item.id), **extra,
    }


def _stored_price(doc):
    return Decimal(doc.content["food_order"]["line_items"][0]["unit_price"])


def _refresh_events(db, doc):
    return db.scalars(
        select(BookingEvent).where(
            BookingEvent.booking_id == doc.booking_id,
            BookingEvent.event_type == "food_prices_refreshed",
        )
    ).all()


# --- the repair ---------------------------------------------------------------


def test_a_stale_unauthored_line_is_repriced_when_sent(db, hamilton, loft, contact, menu_items):
    """THE one. Generated at $230 while the catalogue says $250; nobody
    touched it; it goes out at $250."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Stale")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    assert _stored_price(doc) == stale

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert doc.status == DocumentStatus.sent
    assert _stored_price(doc) == item.current_price, "the draft went out at the withdrawn price"


def test_the_total_block_moves_with_the_lines(db, hamilton, loft, contact, menu_items):
    """Keeping the heading while the items moved is the 2026-09-08 defect:
    a live document whose own items summed to one figure under a heading
    reading another."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Total")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert Decimal(str(doc.content["total_food_spend"]["total"])) == item.current_price * 2


def test_what_moved_is_on_the_audit_trail(db, hamilton, loft, contact, menu_items):
    """A price that changes under a document with no line saying so is the
    same silence this register is about."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Audit")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")

    events = _refresh_events(db, doc)
    assert len(events) == 1
    assert item.name in events[0].old_value
    assert str(stale) in events[0].old_value and str(item.current_price) in events[0].old_value
    assert events[0].new_value == str(doc.version)


def test_the_helper_reports_what_it_moved(db, hamilton, loft, contact, menu_items):
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Return")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])

    moved = beo_proposals.refresh_draft_food_prices(db, doc, actor="test")

    assert len(moved) == 1 and item.name in moved[0]


# --- what is left alone --------------------------------------------------------


def test_a_line_a_person_wrote_is_left_alone_by_the_record(db, hamilton, loft, contact, menu_items):
    """content_authorship names food_order: a person changed it. Their
    figure stands and the nightly check keeps reporting it."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Authored")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    doc.content = content_authorship.record(doc.content, ["food_order"])
    db.flush()
    assert "food_order" in content_authorship.authored(doc.content)

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert _stored_price(doc) == stale, "a person's price was overwritten"
    assert _refresh_events(db, doc) == []


def test_a_line_a_person_wrote_is_left_alone_by_the_audit_trail(db, hamilton, loft, contact, menu_items):
    """No authorship record -- the shape of a draft that predates it -- but
    a document_edited event on THIS version naming food_order. The second
    leg, and the one that catches what the record cannot."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Trail")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    db.add(BookingEvent(
        booking_id=booking.id, event_type="document_edited", field_name="beo_version",
        old_value="dietaries, food_order", new_value=str(doc.version), actor="staff:someone",
    ))
    db.flush()
    assert not content_authorship.has_record(doc.content)

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert _stored_price(doc) == stale
    assert _refresh_events(db, doc) == []


def test_an_edit_to_a_different_field_does_not_protect_the_price(db, hamilton, loft, contact, menu_items):
    """The trail leg matches the FIELD, not merely the existence of an
    edit. A person who fixed the dietaries did not quote a platter."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE OtherField")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    db.add(BookingEvent(
        booking_id=booking.id, event_type="document_edited", field_name="beo_version",
        old_value="dietaries", new_value=str(doc.version), actor="staff:someone",
    ))
    db.flush()

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert _stored_price(doc) == item.current_price


def test_an_edit_on_an_earlier_version_does_not_protect_this_one(db, hamilton, loft, contact, menu_items):
    """Per version, like was_hand_edited: a regenerate that KEEPS the food
    order carries the authorship record across, which the first leg reads.
    A trail entry on some other version says nothing about these lines."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE OldVersion")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    db.add(BookingEvent(
        booking_id=booking.id, event_type="document_edited", field_name="beo_version",
        old_value="food_order", new_value=str(doc.version - 1), actor="staff:someone",
    ))
    db.flush()

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert _stored_price(doc) == item.current_price


def test_a_hand_typed_line_is_untouched(db, hamilton, loft, contact, menu_items):
    """No menu_item_id, nothing to price it against."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Handtyped")
    doc = _draft(db, booking, [
        {"description": "Something bespoke", "quantity": 1, "unit_price": "12.34"},
        _catalogue_line(item, stale),
    ])

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    lines = doc.content["food_order"]["line_items"]
    assert lines[0]["unit_price"] == "12.34"
    assert Decimal(lines[1]["unit_price"]) == item.current_price, "the catalogue line beside it still moved"


def test_a_retired_item_keeps_its_quoted_price(db, hamilton, loft, contact, menu_items):
    """catalogue.get_by_id_any's rule: retirement means no longer offered,
    never that an existing order is unpriceable. Its stored price is the
    only price it has."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Retired")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    item.is_active = False
    db.flush()

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.refresh(doc)

    assert _stored_price(doc) == stale


def test_a_draft_already_at_todays_price_writes_no_event(db, hamilton, loft, contact, menu_items):
    """Nothing moved, nothing to say. An event on every send is noise."""
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, "ZZREPRICE Current")
    doc = _draft(db, booking, [_catalogue_line(item, item.current_price)])

    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")

    assert _refresh_events(db, doc) == []


def test_a_sent_event_order_is_never_repriced(db, hamilton, loft, contact, menu_items):
    """It is the client's quote."""
    item = _a_platter(menu_items)
    stale = item.current_price - Decimal("20.00")
    booking = _booking(db, loft, contact, "ZZREPRICE Sent")
    doc = _draft(db, booking, [_catalogue_line(item, stale)])
    doc.status = DocumentStatus.sent
    db.flush()

    assert beo_proposals.refresh_draft_food_prices(db, doc, actor="test") == []
    assert _stored_price(doc) == stale


# --- the question underneath ------------------------------------------------------


def test_a_person_wrote_the_food_order_answers_both_legs(db, hamilton, loft, contact, menu_items):
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, "ZZREPRICE Legs")
    doc = _draft(db, booking, [_catalogue_line(item, item.current_price)])

    assert beo_proposals.a_person_wrote_the_food_order(db, doc) is False

    doc.content = content_authorship.record(doc.content, ["food_order"])
    db.flush()
    assert beo_proposals.a_person_wrote_the_food_order(db, doc) is True

    doc.content = content_authorship.forget(doc.content, ["food_order"])
    db.flush()
    assert beo_proposals.a_person_wrote_the_food_order(db, doc) is False
    db.add(BookingEvent(
        booking_id=booking.id, event_type="document_edited", field_name="beo_version",
        old_value="food_order", new_value=str(doc.version), actor="staff:someone",
    ))
    db.flush()
    assert beo_proposals.a_person_wrote_the_food_order(db, doc) is True
