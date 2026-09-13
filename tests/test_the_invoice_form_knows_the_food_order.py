"""Create-final-invoice arrives holding the Event Order's food order.

Aaron: "The Event Order already holds the itemised food order with line
prices and a $1,450 subtotal, so the invoice should be derivable from it
rather than re-entered."

WHY A PREFILL AND NOT AN AUTO-CREATE. `sync_final_invoice_from_food` does
build the invoice outright -- but it has exactly one caller, inside the
proposal-approval path, so a booking whose food was typed straight into
the Event Order never reaches it. Every migrated iVvy booking is in that
shape. Making that function fire on any Event Order would start writing
invoices for bookings nobody asked it to, so the form fills itself in and
a person still presses the button: the retyping goes, the decision stays.

THE NAME HAS HAD THREE KEYS -- "description", "item" and "name" -- and the
bookings that need this most are the migrated ones carrying the older
spellings, so the test drives all three rather than only the current one.
"""
import datetime as dt
import re
from decimal import Decimal

import pytest

from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking


def _booking(db, loft, contact, name="Prefill"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=12), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=80, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _beo_with_food(db, booking, lines):
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {"line_items": lines, "note": None}
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="test"
    )
    db.flush()
    return document


# --- the three historical shapes -------------------------------------------


@pytest.mark.parametrize("key", ["description", "item", "name"])
def test_the_food_order_fills_the_form_whichever_key_it_uses(db, hamilton, loft, contact, key):
    """A migrated booking carries the older spelling. Reading only the
    current one would leave exactly the bookings this exists for blank."""
    booking = _booking(db, loft, contact, name=f"Prefill {key}")
    document = _beo_with_food(db, booking, [
        {key: "Grazing Platter", "quantity": 4, "unit_price": "250.00"},
        {key: "Pizza", "quantity": 9, "unit_price": "50.00"},
    ])

    rows = beo_proposals.final_invoice_prefill(document)

    assert [r["description"] for r in rows] == ["Grazing Platter", "Pizza"]
    assert [r["quantity"] for r in rows] == [4, 9]
    assert [r["unit_price"] for r in rows] == ["250.00", "50.00"]


def test_the_rows_add_up_to_the_event_orders_own_total(db, hamilton, loft, contact):
    """Aaron's $1,450. If the form and the document disagreed about the
    total, prefilling would be worse than retyping."""
    booking = _booking(db, loft, contact, name="Adds Up")
    document = _beo_with_food(db, booking, [
        {"description": "Grazing Platter", "quantity": 4, "unit_price": "250.00"},
        {"description": "Pizza", "quantity": 9, "unit_price": "50.00"},
    ])

    rows = beo_proposals.final_invoice_prefill(document)
    total = sum(Decimal(str(r["quantity"])) * Decimal(r["unit_price"]) for r in rows)

    assert total == Decimal("1450.00")


def test_no_event_order_means_the_form_is_unchanged(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="No Order")

    assert beo_proposals.final_invoice_prefill(None) == []
    document = _beo_with_food(db, booking, [])
    assert beo_proposals.final_invoice_prefill(document) == []


def test_a_line_with_no_name_is_skipped_rather_than_blank(db, hamilton, loft, contact):
    """A blank description row is how create_final_invoice is told to skip
    a row, so an unnamed line must not silently become one that counts."""
    booking = _booking(db, loft, contact, name="Unnamed Line")
    document = _beo_with_food(db, booking, [
        {"description": "", "quantity": 2, "unit_price": "100.00"},
        {"description": "Pizza", "quantity": 9, "unit_price": "50.00"},
    ])

    rows = beo_proposals.final_invoice_prefill(document)

    assert [r["description"] for r in rows] == ["Pizza"]


# --- what the page actually renders ----------------------------------------


def test_the_booking_page_form_arrives_filled_in(admin_client, db, hamilton, loft, contact):
    """The assertion is on the rendered form, because a prefill nothing
    renders is the same as no prefill."""
    booking = _booking(db, loft, contact, name="ZZRENDERED Prefill")
    _beo_with_food(db, booking, [
        {"description": "Grazing Platter", "quantity": 4, "unit_price": "250.00"},
    ])

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)

    assert page.status_code == 200
    assert 'value="Grazing Platter"' in page.text, "the food line is not in the form"
    assert 'value="250.00"' in page.text, "the price is not in the form"
    assert "Filled in from this booking's Event Order" in page.text, (
        "nothing tells the staff member where these rows came from"
    )


def test_the_form_still_offers_six_rows_in_total(admin_client, db, hamilton, loft, contact):
    """Prefilled rows come out of the six, not on top of them -- otherwise
    a big food order makes an unusably long form."""
    booking = _booking(db, loft, contact, name="ZZSIXROWS Prefill")
    _beo_with_food(db, booking, [
        {"description": f"Item {n}", "quantity": 1, "unit_price": "10.00"} for n in range(2)
    ])

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)
    form = page.text.split('invoices/final')[1].split("</form>")[0]

    assert len(re.findall(r'name="description"', form)) == 6
