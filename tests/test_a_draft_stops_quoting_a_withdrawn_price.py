"""A draft Event Order quoting a price the catalogue no longer charges.

From the stale-copy register: a food line's unit_price is frozen when the
item is selected, and nothing ever refreshes it or checks it. Move a
catalogue price and every unsent Event Order still quotes the old one --
then the invoice built from that Event Order bills the old one too.

Platters are the live case. catalogue.resolve_price sends everything except
pizzas straight to current_price, so a platter has no legacy variant and no
per-booking lock shielding it; a platter price change reaches every
booking immediately.

DRAFTS ONLY, and REPORTED rather than rewritten. A SENT Event Order's price
is what the client was quoted and must not move underneath them -- flagging
it would be telling staff their own quote is wrong. And a quoted figure is
a commercial decision a staff member may have made deliberately, so this
says so and leaves it alone, like every other check in the module.
"""
import datetime as dt
from decimal import Decimal

from app.models.document import DocumentStatus, DocumentType
from app.services import beo_proposals, documents as documents_service, reconciliation
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Price Drift"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _beo(db, booking, item, quoted, *, send=False):
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {
        "line_items": [{
            "description": item.name, "quantity": 1, "unit_price": str(quoted),
            "category": item.category.value, "menu_item_id": str(item.id),
        }],
        "note": None,
    }
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    if send:
        documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
        db.flush()
    return doc


def _a_platter(menu_items):
    from app.models.menu_item import MenuItemCategory

    return next(i for i in menu_items.values() if i.category == MenuItemCategory.platter)


# --- the drift --------------------------------------------------------------


def test_a_draft_quoting_a_moved_price_is_flagged(db, hamilton, loft, contact, menu_items):
    """THE one. The catalogue moved; the draft did not."""
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, name="ZZDRIFT Draft")
    _beo(db, booking, item, item.current_price - Decimal("20.00"))

    findings = reconciliation.check_food_price_drift(db, [booking])

    assert [f.check_code for f in findings] == ["FOOD_PRICE_DRIFT"]
    assert item.name in findings[0].detail
    assert str(item.current_price) in findings[0].detail


def test_a_draft_quoting_the_current_price_is_silent(db, hamilton, loft, contact, menu_items):
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, name="ZZDRIFT Current")
    _beo(db, booking, item, item.current_price)

    assert reconciliation.check_food_price_drift(db, [booking]) == []


def test_a_sent_event_order_is_never_flagged(db, hamilton, loft, contact, menu_items):
    """It is the client's quote. Telling staff their own quote is wrong is
    not a finding, it is noise -- and acting on it would move a price
    underneath somebody who has already been told it."""
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, name="ZZDRIFT Sent")
    doc = _beo(db, booking, item, item.current_price - Decimal("20.00"), send=True)
    assert doc.status == DocumentStatus.sent

    assert reconciliation.check_food_price_drift(db, [booking]) == []


def test_a_hand_typed_line_is_not_flagged(db, hamilton, loft, contact, menu_items):
    """No menu_item_id means no catalogue item to compare against. A line
    nobody can price is not a line that has drifted."""
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, name="ZZDRIFT Handtyped")
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {
        "line_items": [{"description": "Something bespoke", "quantity": 1, "unit_price": "12.34"}],
        "note": None,
    }
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()

    assert reconciliation.check_food_price_drift(db, [booking]) == []


def test_a_booking_with_no_event_order_is_not_an_error(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZDRIFT None")

    assert reconciliation.check_food_price_drift(db, [booking]) == []


def test_it_reports_and_does_not_rewrite(db, hamilton, loft, contact, menu_items):
    """Reads everything, fixes nothing -- the module's own rule. A quoted
    figure may have been set deliberately."""
    item = _a_platter(menu_items)
    booking = _booking(db, loft, contact, name="ZZDRIFT NoRewrite")
    quoted = item.current_price - Decimal("20.00")
    doc = _beo(db, booking, item, quoted)

    reconciliation.check_food_price_drift(db, [booking])

    db.refresh(doc)
    stored = doc.content["food_order"]["line_items"][0]["unit_price"]
    assert Decimal(stored) == quoted, "the check rewrote a quoted price"
