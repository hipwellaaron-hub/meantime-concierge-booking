"""An approved food order reaches an invoice that has already gone out,
in one reviewed step instead of five.

Aaron, 2026-09-14: "the food order flowing through from an approved
proposal into the Event Order without me correcting it afterwards. That's
the one piece of the original job that still has my hands in it."

The Event Order half already worked. The invoice half did not, and Adam
Williams' own trail is the specification:

    00:16:15  beo_proposal_approved     food_order  (correct, unedited)
    00:16:16  final_invoice_from_beo    "final invoice #1055 is already
                                         sent and was left alone --
                                         revise it by hand"
    00:17:44  invoice_status_changed    sent -> cancelled     <- by hand
    00:17:45  invoice_created           950.00                <- by hand
    00:17:51  invoice_edited                                  <- by hand
    00:22:25  invoice_edited                                  <- by hand
    00:23:00  invoice_status_changed    draft -> sent         <- by hand

Five steps, because Revise reopens an invoice with ITS OWN lines -- it
hands back the invoice that is already wrong. Reissue starts the draft
from the approved order instead.

STILL A PERSON'S DECISION, and deliberately. Cancelling a sent invoice
kills the link a client is holding and issues a new number. What is
removed is the five steps after the decision, not the decision.

EVERY PROBE SENDS THE INVOICE FIRST. A draft is refreshed automatically by
sync_final_invoice_from_food, so a draft would come out right whatever
this does.
"""
import datetime as dt
from decimal import Decimal

from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import beo_proposals, documents as documents_service, invoicing
from app.services.booking import change_status, create_booking

OWN = beo_proposals.LINE_SOURCE_PROPOSAL


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    change_status(db, b, BookingStatus.confirmed, actor="test")
    return b


def _beo_with_food(db, booking, pairs, menu_items):
    """An Event Order holding exactly these (item name, qty) as its food
    order, in the shape an approval writes."""
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {
        "line_items": [
            {
                "description": menu_items[name].name,
                "quantity": qty,
                "unit_price": str(menu_items[name].current_price),
                "category": menu_items[name].category.value,
                "menu_item_id": str(menu_items[name].id),
                "source": OWN,
            }
            for name, qty in pairs
        ],
        "note": None,
    }
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()
    return doc


def _sent_final(db, booking, pairs, menu_items):
    invoice = invoicing.create_final_invoice(
        db, booking,
        line_items=[
            {
                "description": menu_items[name].name, "quantity": qty,
                "unit_price": str(menu_items[name].current_price),
                "menu_item_id": str(menu_items[name].id), "source": OWN,
            }
            for name, qty in pairs
        ],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    invoicing.mark_sent(db, invoice, actor="staff:test@meantime.com.au")
    db.flush()
    return invoice


def _charges(invoice):
    return [
        (li["description"], str(li["quantity"]))
        for li in invoice.line_items
        if li.get("description") != invoicing.DEPOSIT_CREDIT_DESCRIPTION
    ]


# --- what is out of step --------------------------------------------------------


def test_a_sent_invoice_billing_the_old_order_is_found(db, hamilton, loft, contact, menu_items):
    """THE one, in Adam's exact shape: the invoice bills six platters at 2,
    the Event Order now holds the order he actually placed."""
    booking = _booking(db, loft, contact, "ZZREISSUE Found")
    _sent_final(db, booking, [("Grazing Platter", 2), ("Pork Belly Bites", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1), ("Pork Belly Bites", 3)], menu_items)

    out_of_step = beo_proposals.sent_final_invoice_out_of_step(db, booking)

    assert out_of_step is not None


def test_an_invoice_that_matches_is_not_offered(db, hamilton, loft, contact, menu_items):
    """The positive control. A button that always shows is a button that
    gets pressed on an invoice that was right."""
    booking = _booking(db, loft, contact, "ZZREISSUE Matches")
    pairs = [("Grazing Platter", 1), ("Pork Belly Bites", 3)]
    _sent_final(db, booking, pairs, menu_items)
    _beo_with_food(db, booking, pairs, menu_items)

    assert beo_proposals.sent_final_invoice_out_of_step(db, booking) is None


def test_a_different_order_of_the_same_total_is_still_out_of_step(db, hamilton, loft, contact, menu_items):
    """COMPARED LINE BY LINE, never by total. Adam's wrong order and his
    real one both came to $1,450, so a total comparison called the invoice
    correct -- which is how it went out."""
    booking = _booking(db, loft, contact, "ZZREISSUE SameTotal")
    _sent_final(db, booking, [("Pork Belly Bites", 2), ("Chicken Tender Skewers", 2)], menu_items)
    _beo_with_food(db, booking, [("Pork Belly Bites", 3), ("Chicken Tender Skewers", 1)], menu_items)
    invoice = beo_proposals.sent_final_invoice_out_of_step(db, booking)

    assert invoice is not None, "an order of the same total but different lines was called correct"


def test_a_part_paid_invoice_is_never_offered(db, hamilton, loft, contact, menu_items):
    """Money against it makes it a refund question, not a reissue --
    revise_sent_invoice refuses it too, and the button must not appear
    over a refusal."""
    booking = _booking(db, loft, contact, "ZZREISSUE PartPaid")
    invoice = _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)
    invoicing.record_payment(
        db, invoice, amount=Decimal("50.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()

    assert beo_proposals.sent_final_invoice_out_of_step(db, booking) is None


def test_a_draft_invoice_is_not_offered(db, hamilton, loft, contact, menu_items):
    """A draft is refreshed by the sync automatically. Offering a reissue
    would be offering to cancel something nobody has seen."""
    booking = _booking(db, loft, contact, "ZZREISSUE Draft")
    invoicing.create_final_invoice(
        db, booking,
        line_items=[{"description": "Food", "quantity": 1, "unit_price": "100.00", "source": OWN}],
        due_date=dt.date.today() + dt.timedelta(days=20), actor="test",
    )
    db.flush()
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)

    assert beo_proposals.sent_final_invoice_out_of_step(db, booking) is None


def test_no_event_order_means_nothing_to_reissue_from(db, hamilton, loft, contact, menu_items):
    booking = _booking(db, loft, contact, "ZZREISSUE NoBEO")
    _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)

    assert beo_proposals.sent_final_invoice_out_of_step(db, booking) is None


# --- the one step ---------------------------------------------------------------


def test_the_reissue_cancels_the_old_and_drafts_the_approved_order(db, hamilton, loft, contact, menu_items):
    """THE one. One call replaces cancel, create, edit, edit."""
    booking = _booking(db, loft, contact, "ZZREISSUE OneStep")
    old = _sent_final(db, booking, [("Grazing Platter", 2), ("Pork Belly Bites", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1), ("Pork Belly Bites", 3)], menu_items)

    draft = beo_proposals.reissue_final_invoice_from_food(db, booking, actor="staff:aaron@meantime.com.au")

    db.refresh(old)
    assert old.status == InvoiceStatus.cancelled
    assert draft.status == InvoiceStatus.draft, "the reissue sent it -- sending is the client-facing act"
    assert _charges(draft) == [("Grazing Platter", "1"), ("Pork Belly Bites", "3")]
    assert draft.invoice_reference != old.invoice_reference


def test_the_new_draft_is_the_syncs_to_refresh(db, hamilton, loft, contact, menu_items):
    """It carries the ownership mark, so a LATER approval refreshes it
    instead of raising the same banner again."""
    booking = _booking(db, loft, contact, "ZZREISSUE Owned")
    _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)

    draft = beo_proposals.reissue_final_invoice_from_food(db, booking, actor="staff:aaron@meantime.com.au")

    assert beo_proposals._is_catalogue_built(draft) is True


def test_the_deposit_credit_is_re_derived_on_the_new_draft(db, hamilton, loft, contact, menu_items):
    """create_final_invoice applies it from what has actually been paid at
    reissue time, so a deposit paid since the old invoice went out lands
    on the new one."""
    booking = _booking(db, loft, contact, "ZZREISSUE Credit")
    unit = menu_items["Grazing Platter"].current_price
    # Sent at 3 platters, no deposit paid yet -- so it carries no credit.
    old = _sent_final(db, booking, [("Grazing Platter", 3)], menu_items)
    assert old.total == unit * 3, "fixture: the sent invoice must start uncredited"

    # THEN the deposit lands, and THEN the order changes to 4.
    deposit = invoicing.create_deposit_invoice(db, booking, due_date=dt.date.today(), actor="test")
    db.flush()
    invoicing.mark_sent(db, deposit, actor="staff:test@meantime.com.au")
    invoicing.record_payment(
        db, deposit, amount=Decimal("500.00"), method=PaymentMethod.bank_transfer, actor="test"
    )
    db.flush()
    _beo_with_food(db, booking, [("Grazing Platter", 4)], menu_items)

    draft = beo_proposals.reissue_final_invoice_from_food(db, booking, actor="staff:aaron@meantime.com.au")

    credit = [li for li in draft.line_items if li["description"] == invoicing.DEPOSIT_CREDIT_DESCRIPTION]
    assert len(credit) == 1, "the new draft carries no deposit credit"
    assert Decimal(str(credit[0]["unit_price"])) == Decimal("-500.00")
    assert draft.total == unit * 4 - Decimal("500.00"), (
        f"four platters at {unit} less the $500 deposit, got {draft.total}"
    )


def test_it_refuses_when_nothing_is_out_of_step(db, hamilton, loft, contact, menu_items):
    """A reissue that cancels a correct invoice is a defect with a button."""
    import pytest

    booking = _booking(db, loft, contact, "ZZREISSUE Refuses")
    pairs = [("Grazing Platter", 1)]
    invoice = _sent_final(db, booking, pairs, menu_items)
    _beo_with_food(db, booking, pairs, menu_items)

    with pytest.raises(ValueError, match="nothing to reissue"):
        beo_proposals.reissue_final_invoice_from_food(db, booking, actor="staff:aaron@meantime.com.au")

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent, "it cancelled an invoice it refused to reissue"


def test_what_happened_is_on_the_trail(db, hamilton, loft, contact, menu_items):
    from app.models import BookingEvent

    booking = _booking(db, loft, contact, "ZZREISSUE Trail")
    old = _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)

    draft = beo_proposals.reissue_final_invoice_from_food(db, booking, actor="staff:aaron@meantime.com.au")

    events = [
        e for e in db.query(BookingEvent).filter_by(booking_id=booking.id).all()
        if e.event_type == "final_invoice_reissued"
    ]
    assert len(events) == 1
    assert old.invoice_reference in events[0].old_value
    assert draft.invoice_reference in events[0].new_value


# --- and the page offers it ------------------------------------------------------


def test_the_button_appears_on_the_invoice_that_is_out_of_step(admin_client, db, loft, contact, menu_items):
    booking = _booking(db, loft, contact, "ZZREISSUE Button")
    _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)

    page = admin_client.get(f"/admin/bookings/{booking.id}", follow_redirects=True)

    assert page.status_code == 200
    assert "Reissue from food order" in page.text
    assert f"/bookings/{booking.id}/invoices/reissue-from-food" in page.text


def test_the_button_is_absent_when_the_invoice_matches(admin_client, db, loft, contact, menu_items):
    booking = _booking(db, loft, contact, "ZZREISSUE NoButton")
    pairs = [("Grazing Platter", 1)]
    _sent_final(db, booking, pairs, menu_items)
    _beo_with_food(db, booking, pairs, menu_items)

    page = admin_client.get(f"/admin/bookings/{booking.id}", follow_redirects=True)

    assert "Reissue from food order" not in page.text


def test_the_route_lands_on_the_new_drafts_editor(admin_client, db, loft, contact, menu_items):
    import re

    booking = _booking(db, loft, contact, "ZZREISSUE Route")
    old = _sent_final(db, booking, [("Grazing Platter", 2)], menu_items)
    _beo_with_food(db, booking, [("Grazing Platter", 1)], menu_items)
    page = admin_client.get(f"/admin/bookings/{booking.id}", follow_redirects=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    resp = admin_client.post(
        f"/admin/hamilton/bookings/{booking.id}/invoices/reissue-from-food",
        data={"csrf_token": csrf}, follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    assert "/invoices/" in resp.headers["location"] and resp.headers["location"].endswith("/edit")
    db.refresh(old)
    assert old.status == InvoiceStatus.cancelled


def test_the_route_refuses_rather_than_cancelling_a_correct_invoice(admin_client, db, loft, contact, menu_items):
    import re

    booking = _booking(db, loft, contact, "ZZREISSUE RouteRefuses")
    pairs = [("Grazing Platter", 1)]
    invoice = _sent_final(db, booking, pairs, menu_items)
    _beo_with_food(db, booking, pairs, menu_items)
    page = admin_client.get(f"/admin/bookings/{booking.id}", follow_redirects=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    resp = admin_client.post(
        f"/admin/hamilton/bookings/{booking.id}/invoices/reissue-from-food",
        data={"csrf_token": csrf}, follow_redirects=False,
    )

    assert resp.status_code == 409
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent
