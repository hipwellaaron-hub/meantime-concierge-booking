"""An invoice keeps its catalogue ids through a form round trip.

Aaron, 2026-09-14: "This one bothers me more than its size suggests,
because the hazard is triggered by being careful. Opening the invoice edit
form to check it and pressing Save de-syncs the booking, without changing a
character and without telling me."

WHY IT MATTERS. beo_proposals._is_catalogue_built refreshes a draft final
invoice only while EVERY charge line carries a menu_item_id -- that is the
signature of an invoice this system built from an approved food order. Lose
one id and the booking is permanently outside auto-sync: every later
approval logs "carries lines this Event Order did not put there, so it was
left alone".

WHAT WAS WRONG. _parse_invoice_line_items built {description, quantity,
unit_price} and nothing else, and neither invoice template posted an id
back -- so the form could not return what it was never given. A no-op Save
stripped every id. The identical hazard was found and fixed on the Event
Order edit form and never carried across.

AND THE PREFILL had the same hole: final_invoice_prefill read the Event
Order's food lines and dropped the id, so an invoice created from the
booking page's prefilled form had never been catalogue-built. It only
looked as though it came from the Event Order.
"""
import datetime as dt
import re
from decimal import Decimal

from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service, invoicing
from app.services.booking import create_booking

MENU_ID = "f9cd3dbc-0000-4000-8000-00000000beef"


def _booking(db, loft, contact, name="Catalogue Ids"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=15), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _beo_with_catalogue_food(db, booking):
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {
        "line_items": [
            {"description": "Grazing Platter", "quantity": 1, "unit_price": "250.00",
             "category": "platter", "menu_item_id": MENU_ID},
        ],
        "note": None,
    }
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    return doc


# --- the prefill ------------------------------------------------------------


def test_the_prefill_carries_the_catalogue_id(db, hamilton, loft, contact):
    """Without this, an invoice built from the Event Order's own food order
    was never catalogue-built -- it only looked as though it came from
    there."""
    booking = _booking(db, loft, contact, name="ZZPREFILL Ids")
    document = _beo_with_catalogue_food(db, booking)

    rows = beo_proposals.final_invoice_prefill(document)

    assert [r["menu_item_id"] for r in rows] == [MENU_ID]


def test_the_prefilled_form_posts_the_id_back(admin_client, db, hamilton, loft, contact):
    """Rendered, because an id the form holds but never posts is the same
    as no id."""
    booking = _booking(db, loft, contact, name="ZZPREFILLFORM Ids")
    _beo_with_catalogue_food(db, booking)

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)
    form = page.text.split("invoices/final")[1].split("</form>")[0]

    assert f'name="menu_item_id" value="{MENU_ID}"' in form, (
        "the prefilled row does not post its catalogue id back"
    )
    # Every row posts one, blank included -- the arrays are positional.
    assert len(re.findall(r'name="menu_item_id"', form)) == len(
        re.findall(r'name="description"', form)
    ), "the id array is shorter than the description array"


# --- the round trip that used to strip them --------------------------------


def test_a_no_op_save_keeps_the_ids(admin_client, db, hamilton, loft, contact):
    """THE one. Open the edit form, change nothing, press Save."""
    booking = _booking(db, loft, contact, name="ZZNOOP Save")
    invoice = invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Grazing Platter", "quantity": 1,
                     "unit_price": "250.00", "menu_item_id": MENU_ID}],
        due_date=dt.date.today() + dt.timedelta(days=10), actor="test",
    )
    db.flush()
    assert beo_proposals._is_catalogue_built(invoice) is True

    page = admin_client.get(
        f"/admin/hamilton/bookings/{booking.id}/invoices/{invoice.id}/edit", follow_redirects=True
    )
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    # Exactly what the form renders back, unchanged -- including the blank
    # discount row, which is what a real no-op Save posts.
    descriptions = re.findall(r'name="description" value="([^"]*)"', page.text)
    quantities = re.findall(r'name="quantity" value="([^"]*)"', page.text)
    prices = re.findall(r'name="unit_price" value="([^"]*)"', page.text)
    ids = re.findall(r'name="menu_item_id" value="([^"]*)"', page.text)

    resp = admin_client.post(
        f"/admin/hamilton/bookings/{booking.id}/invoices/{invoice.id}/edit",
        data={
            "csrf_token": csrf,
            "due_date": invoice.due_date.isoformat(),
            "description": descriptions,
            "quantity": quantities,
            "unit_price": prices,
            "menu_item_id": ids,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.status_code

    db.refresh(invoice)
    charge = [li for li in invoice.line_items if not li["description"].startswith("Less:")]
    assert [li.get("menu_item_id") for li in charge] == [MENU_ID], (
        "a save with nothing changed stripped the catalogue id"
    )
    assert beo_proposals._is_catalogue_built(invoice) is True, (
        "the booking fell out of auto-sync by being looked at"
    )


# --- and the things that must NOT happen ------------------------------------


def test_a_retyped_description_does_not_keep_a_stale_id(db, hamilton, loft, contact):
    """The id is carried forward, never re-derived. A staff member who
    retypes a description meant to change the line, and guessing the id
    back would reattach it to an item it no longer names."""
    from app.api.admin_bookings import _parse_invoice_line_items

    rows = _parse_invoice_line_items(
        ["Something else entirely"], ["1"], ["100.00"], [""],
    )
    assert "menu_item_id" not in rows[0]


def test_a_short_id_array_does_not_truncate_the_charge_lines(db, hamilton, loft, contact):
    """The id is a hidden field, so an older cached page or a row added by
    the Add-line button can post fewer ids than descriptions. zip() would
    have silently dropped real charge lines -- money off the invoice
    because a hidden input was missing."""
    from app.api.admin_bookings import _parse_invoice_line_items

    rows = _parse_invoice_line_items(
        ["Platter", "Room hire", "Bar tab"], ["1", "1", "1"], ["250.00", "500.00", "300.00"],
        [MENU_ID],
    )

    assert [r["description"] for r in rows] == ["Platter", "Room hire", "Bar tab"]
    assert rows[0]["menu_item_id"] == MENU_ID
    assert "menu_item_id" not in rows[1] and "menu_item_id" not in rows[2]


def test_no_id_array_at_all_still_parses(db, hamilton, loft, contact):
    """The array is optional: a caller or a form that never renders it must
    keep working, because the alternative is a 422 on a form that used to
    save."""
    from app.api.admin_bookings import _parse_invoice_line_items

    rows = _parse_invoice_line_items(["Platter"], ["1"], ["250.00"])
    assert rows == [{"description": "Platter", "quantity": "1", "unit_price": "250.00"}]


def test_a_blank_row_does_not_consume_the_next_rows_id(db, hamilton, loft, contact):
    """The arrays are positional and blank rows are skipped, so the id has
    to be indexed by ROW rather than by output position -- otherwise every
    id after a blank row attaches to the wrong line."""
    from app.api.admin_bookings import _parse_invoice_line_items

    rows = _parse_invoice_line_items(
        ["", "Grazing Platter"], ["1", "1"], ["0.00", "250.00"], ["", MENU_ID],
    )

    assert len(rows) == 1
    assert rows[0]["description"] == "Grazing Platter"
    assert rows[0]["menu_item_id"] == MENU_ID, "the id slid onto the wrong row"
