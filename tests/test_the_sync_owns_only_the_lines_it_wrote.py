"""The approval sync rebuilds only the invoice lines it wrote -- and knows
which those are by a mark of its own, not by a key that meant something
else.

_is_catalogue_built used to ask "does every charge line carry a
menu_item_id?", on the stated grounds that nothing else wrote one: the
staff form and the wizard's line builder both left it out, so the id
doubled as an ownership mark. On 2026-09-14 the wizard started naming its
catalogue item so a price move could reach its lines -- and the moment a
second writer produced the key, an approval would have rebuilt a
wizard-built invoice and dropped its in-house cake.
test_a_wizard_built_draft_is_left_alone_rather_than_double_billed caught it.

One key was carrying two meanings: "priced from this item" and "the sync
wrote this". They are two keys now. `source` is written only by the sync
(onto the Event Order in _apply, onto the invoice in
sync_final_invoice_from_food), carried by the prefill and by both invoice
forms and the Event Order editor on the same terms as the id, and it is
the only thing _is_catalogue_built reads.

WHY THE ROUND TRIPS MATTER, beyond ownership: content_authorship records
the food order as a PERSON'S whenever the posted dict differs from the
stored one. A form that dropped `source` would make every no-op save look
like a hand edit -- and refresh_draft_food_prices would then freeze a
stale price at send on a document nobody typed into.
"""
import datetime as dt
from decimal import Decimal

from app.api.admin_bookings import _food_order_from_form, _parse_invoice_line_items
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.beo_proposals import LINE_SOURCE_PROPOSAL, _is_catalogue_built
from app.services.booking import create_booking
from app.services.wizard_generation import build_food_line_items


class _Invoice:
    def __init__(self, line_items):
        self.line_items = line_items


def _sync_line(item, qty=2):
    return {
        "description": item.name, "quantity": qty, "unit_price": str(item.current_price),
        "category": item.category.value, "menu_item_id": str(item.id), "source": LINE_SOURCE_PROPOSAL,
    }


# --- what the sync owns -------------------------------------------------------------


def test_lines_the_sync_wrote_are_its_to_rebuild(menu_items):
    grazing = menu_items["Grazing Platter"]
    assert _is_catalogue_built(_Invoice([_sync_line(grazing)])) is True


def test_a_wizard_line_carries_an_id_and_is_still_not_the_syncs(db, hamilton, loft, contact, menu_items):
    """THE one. The wizard now names its item; that must not make its
    invoice look like the sync's."""
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date.today() + dt.timedelta(days=30),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="ZZOWN Wizard", event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    grazing = menu_items["Grazing Platter"]
    lines, _ = build_food_line_items(db, booking, {"platters": [{"menu_item_id": str(grazing.id), "quantity": 2}]})

    assert all(ln.get("menu_item_id") for ln in lines), "fixture: the wizard names its item"
    assert _is_catalogue_built(_Invoice(lines)) is False, "a wizard-built invoice would be rebuilt on approval"


def test_a_staff_typed_line_is_not_the_syncs(menu_items):
    assert _is_catalogue_built(_Invoice([{"description": "Room hire", "quantity": 1, "unit_price": "200.00"}])) is False


def test_one_foreign_line_makes_the_whole_invoice_not_the_syncs(menu_items):
    """Replacing the lines would delete the foreign one."""
    grazing = menu_items["Grazing Platter"]
    lines = [_sync_line(grazing), {"description": "Bar tab", "quantity": 1, "unit_price": "300.00"}]
    assert _is_catalogue_built(_Invoice(lines)) is False


def test_an_empty_draft_is_the_syncs(menu_items):
    """Nothing on it to lose; refusing to fill it would raise a banner
    asking somebody to reconcile an invoice with nothing on it."""
    assert _is_catalogue_built(_Invoice([])) is True


# --- the mark survives a round trip -------------------------------------------------


def test_the_invoice_form_parser_carries_the_source(menu_items):
    grazing = menu_items["Grazing Platter"]
    parsed = _parse_invoice_line_items(
        ["Grazing Platter"], ["2"], ["250.00"], [str(grazing.id)], [LINE_SOURCE_PROPOSAL]
    )
    assert parsed[0]["source"] == LINE_SOURCE_PROPOSAL
    assert _is_catalogue_built(_Invoice(parsed)) is True


def test_the_invoice_form_parser_never_mints_a_source():
    """Carried forward only. A form that posts none -- an older cached
    page, a row added with Add-line -- yields a line with no mark."""
    parsed = _parse_invoice_line_items(["Room hire"], ["1"], ["200.00"])
    assert "source" not in parsed[0]
    parsed = _parse_invoice_line_items(["Room hire"], ["1"], ["200.00"], [""], [""])
    assert "source" not in parsed[0]


def test_the_invoice_form_parser_tolerates_a_short_source_array(menu_items):
    """Positional against the ROW, so a short array cannot slide a mark
    onto the wrong line or truncate the line set."""
    grazing = menu_items["Grazing Platter"]
    parsed = _parse_invoice_line_items(
        ["Grazing Platter", "Room hire"], ["2", "1"], ["250.00", "200.00"],
        [str(grazing.id), ""], [LINE_SOURCE_PROPOSAL],
    )
    assert len(parsed) == 2
    assert parsed[0]["source"] == LINE_SOURCE_PROPOSAL
    assert "source" not in parsed[1]


def test_the_event_order_editor_carries_the_source(menu_items):
    grazing = menu_items["Grazing Platter"]
    order = _food_order_from_form(
        ["Grazing Platter"], ["2"], ["250.00"], ["platter"], [str(grazing.id)], [LINE_SOURCE_PROPOSAL], strict=True
    )
    assert order["line_items"][0]["source"] == LINE_SOURCE_PROPOSAL


def test_a_no_op_event_order_save_reproduces_the_stored_line(menu_items):
    """If the posted dict differed from the stored one, content_authorship
    would record the food order as a person's and the send-time re-price
    would treat a generated price as a quote."""
    grazing = menu_items["Grazing Platter"]
    stored = {
        "description": "Grazing Platter", "quantity": 2, "unit_price": "250.00",
        "category": "platter", "menu_item_id": str(grazing.id), "source": LINE_SOURCE_PROPOSAL,
    }
    reposted = _food_order_from_form(
        [stored["description"]], [str(stored["quantity"])], [stored["unit_price"]],
        [stored["category"]], [stored["menu_item_id"]], [stored["source"]], strict=True,
    )["line_items"][0]

    assert reposted == stored, f"a no-op save changes the stored line: {reposted} != {stored}"


def test_the_prefill_carries_the_source(db, hamilton, loft, contact, menu_items):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date.today() + dt.timedelta(days=30),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="ZZOWN Prefill", event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    grazing = menu_items["Grazing Platter"]
    content = beo_proposals.fresh_beo_content(db, booking)
    content["food_order"] = {"line_items": [_sync_line(grazing)], "note": None}
    doc = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    db.flush()

    rows = beo_proposals.final_invoice_prefill(doc)

    assert rows[0]["source"] == LINE_SOURCE_PROPOSAL
    assert rows[0]["menu_item_id"] == str(grazing.id)
