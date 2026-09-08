"""The food order is the one protected field that is money.

Every other protected field is words: losing them costs information. Losing
a hand-added food line costs the venue the money on it, and the client a
document that disagrees with their invoice. Aaron, 2026-09-08: "It's the one
field where a silent rebuild costs money rather than just information."

Two things had to be true at once, and neither is free:

  - a food order is a DICT, and the default renderer answers "" for anything
    that is not a str. Adding the name to the protected table without a
    renderer compares "" against "" for ever, skips every time, and reads as
    protection on every screen while protecting nothing;

  - the wizard cannot simply KEEP it the way it keeps prose. The same
    submission cuts the final invoice from the client's NEW selections, so a
    kept old food order leaves the Event Order and the invoice naming
    different food at different money. That is worse than the loss it would
    prevent, and it lands on the client. So the wizard rebuilds the food
    order and says so in the outstanding items instead.

The staff regenerate has no such problem -- it cuts no invoice -- so there
the food order behaves like every other protected field: named on the
confirmation screen, kept if the person says keep.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.booking import change_status, create_booking
from app.services.document_generation import compute_food_order_total, generate_beo_content

from tests.test_wizard_generation import _complete_all_steps, _make_booking, _pay_deposit

HAND_ADDED = {"description": "Oyster station (negotiated with Aaron)", "quantity": 4, "unit_price": "180.00"}
GRAZING = {"description": "Grazing Platter", "quantity": 2, "unit_price": "250.00"}


# --- the renderer, which is what makes the protection real --------------------


def test_a_food_order_renders_as_the_money_it_commits_to():
    rendered = dr._render_food_order({"line_items": [GRAZING, HAND_ADDED], "note": None})

    assert rendered == "2 x Grazing Platter @ 250\n4 x Oyster station (negotiated with Aaron) @ 180"


def test_it_does_not_render_as_nothing():
    """The whole defect in one assertion. The default renderer answers "" for
    a dict, so without this the field is in the table and inert."""
    assert dr._render_food_order({"line_items": [GRAZING], "note": None}) != ""
    assert dr._render_text({"line_items": [GRAZING], "note": None}) == "", "the default, for contrast"


@pytest.mark.parametrize(
    "other",
    [
        {"line_items": [HAND_ADDED, GRAZING], "note": None},                      # reordered
        {"line_items": [GRAZING, HAND_ADDED], "note": "anything at all"},          # note differs
        {"line_items": [{**GRAZING, "quantity": "2", "unit_price": "250"}, HAND_ADDED], "note": None},
        {"line_items": [{**GRAZING, "category": ""}, HAND_ADDED], "note": None},   # absent == ""
    ],
)
def test_what_is_not_a_loss(other):
    """A reorder, the generator's own note, "250" against "250.00", and an
    empty category against no category key at all are the same food order.
    A warning with no money behind it is noise on the one screen that has
    to stay worth reading."""
    assert dr._render_food_order(other) == dr._render_food_order(
        {"line_items": [GRAZING, HAND_ADDED], "note": None}
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"line_items": [{**GRAZING, "quantity": 3}], "note": None},
        {"line_items": [{**GRAZING, "unit_price": "260.00"}], "note": None},
        {"line_items": [{**GRAZING, "description": "Something else"}], "note": None},
        {"line_items": [{**GRAZING, "category": "platter"}], "note": None},
        {"line_items": [], "note": None},
    ],
)
def test_what_is_a_loss(changed):
    """The category case MOVED here from the list above, on review.

    It was filed as "which heading a line prints under", i.e. cosmetic. It
    is not: document.html groups the client's line items by category and an
    empty one falls into the uncategorised group, so a changed category
    moves a platter out from under Platters on their Event Order. And the
    fingerprint compares the stored dict, so a colleague changing only a
    category refused the save ANYWAY -- while the table, reading this
    renderer, came back empty under text saying nothing would read
    differently. Refusing and then declining to say why is worse than
    either warning or allowing it.
    """
    assert dr._render_food_order(changed) != dr._render_food_order({"line_items": [GRAZING], "note": None})


def test_an_empty_food_order_is_nothing_to_warn_about():
    empty = {"line_items": [], "note": "[REVIEW] no food order captured yet"}
    assert dr._render_food_order(empty) == ""
    assert dr._is_disposable(dr._render_food_order(empty))


def test_a_price_is_never_rendered_in_scientific_notation():
    """str(Decimal("250.00").normalize()) is "2.5E+2". That belongs on no
    confirmation screen."""
    assert "E+" not in dr._render_food_order({"line_items": [GRAZING], "note": None})


# --- the wizard: rebuilt and named, never silently kept ------------------------


@pytest.fixture()
def wizard_client(db, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _booking_with_hand_edited_food(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking)
    content = generate_beo_content(booking, [GRAZING, HAND_ADDED], deposit_paid=Decimal("0.00"))
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:aaron")
    return booking


def test_the_event_order_and_the_invoice_name_the_same_food(db, loft, menu_items, wizard_client):
    """THE load-bearing one. Assert the agreement between the two documents,
    not the presence of a message -- a message can be right while the
    numbers diverge some other way."""
    booking = _booking_with_hand_edited_food(db, loft, menu_items)
    from app.services import wizard as wizard_service

    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    response = wizard_client.post(f"/w/{session.access_token}/review", json={})
    assert response.status_code == 200, response.text

    db.expire_all()
    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    from app.models.invoice import Invoice, InvoiceType

    invoice = db.query(Invoice).filter_by(booking_id=booking.id, type=InvoiceType.final).one()

    beo_lines = beo.content["food_order"]["line_items"]
    # The invoice also carries a "Less: deposit credited" line, which is not
    # food and belongs only on the invoice. Compare the food.
    invoice_food = [
        line for line in invoice.line_items if not str(line.get("unit_price", "")).startswith("-")
    ]
    assert beo_lines == invoice_food, "the Event Order and the invoice list different food"
    assert Decimal(beo.content["total_food_spend"]["total"]) == compute_food_order_total(beo_lines)
    assert not any(
        line.get("description") == HAND_ADDED["description"] for line in beo_lines
    ), "the hand-added line was kept and the invoice does not have it"


def test_staff_are_told_the_hand_edited_food_order_is_gone(db, loft, menu_items, wizard_client):
    """Not keeping it is only defensible because it is said out loud."""
    booking = _booking_with_hand_edited_food(db, loft, menu_items)
    from app.services import wizard as wizard_service

    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    body = wizard_client.post(f"/w/{session.access_token}/review", json={}).json()

    assert body["is_clean"] is False, "a dropped food order must not read as a clean submission"
    assert any("hand-edited" in item and "re-add them to both" in item for item in body["outstanding_items"]), (
        body["outstanding_items"]
    )


def test_the_prose_keep_is_untouched(db, loft, menu_items, wizard_client):
    """Words are still kept. Only the food order is rebuilt, and only because
    of the invoice."""
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 7))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking)
    content = generate_beo_content(booking, [GRAZING, HAND_ADDED], deposit_paid=Decimal("0.00"))
    content["dietaries"] = "1x severe nut allergy (table 4). Kitchen briefed."
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:aaron")
    from app.services import wizard as wizard_service

    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    body = wizard_client.post(f"/w/{session.access_token}/review", json={}).json()

    db.expire_all()
    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert beo.content["dietaries"] == "1x severe nut allergy (table 4). Kitchen briefed."
    assert any("Dietaries kept from the previous" in item for item in body["outstanding_items"])
    assert any("hand-edited" in item for item in body["outstanding_items"])


# --- the staff regenerate, where keeping IS the right answer -------------------


def _csrf(client, booking_id):
    import re

    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_the_regenerate_screen_names_the_food_order(admin_client, db, loft):
    contact = Contact(name="Food Regen", email="foodregen@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Food Regen",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking, [GRAZING, HAND_ADDED], deposit_paid=Decimal("0.00"))
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    shown = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate", data={"csrf_token": csrf}
    )

    assert shown.status_code == 409
    assert "Food order" in shown.text
    assert "Oyster station (negotiated with Aaron)" in shown.text


def test_keeping_it_keeps_the_total_with_it(admin_client, db, loft):
    """The companion. Without it the document's own lines summed to one
    figure under a heading reading another."""
    import re

    contact = Contact(name="Food Keep", email="foodkeep@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 15),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Food Keep",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking, [GRAZING, HAND_ADDED], deposit_paid=Decimal("0.00"))
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")

    csrf = _csrf(admin_client, booking.id)
    shown = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate", data={"csrf_token": csrf}
    )
    expect = re.search(r'name="expect" value="([^"]+)"', shown.text).group(1)
    keep = re.findall(r'name="keep" value="([^"]+)" checked', shown.text)
    assert "food_order" in keep

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": keep},
    )
    assert resp.status_code in (200, 303)

    db.expire_all()
    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    kept = current.content["food_order"]["line_items"]
    assert any(line["description"] == HAND_ADDED["description"] for line in kept)
    assert Decimal(current.content["total_food_spend"]["total"]) == compute_food_order_total(kept), (
        "the document states two different food totals"
    )


# --- the conflict screen, which the widened fingerprint makes reachable --------


def test_a_refused_save_names_the_food_order_and_hands_back_your_own_lines(db, loft, admin_client):
    """Widening the fingerprint without teaching the refusal screen about the
    submitted food order turns a silent revert into a visible one that STILL
    eats the staff member's typing: the table names nothing and the form comes
    back holding the colleague's lines instead of theirs.

    Both halves are asserted here because the mutation that removes the
    submitted food order survived a test that only checked the status code
    and a substring -- "Food order" is a heading on the form below, so the
    page contains it either way.
    """
    import re

    from app.models.document import DocumentStatus

    contact = Contact(name="Food Conflict", email="foodconflict@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 16),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Food Conflict",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking, [GRAZING], deposit_paid=Decimal("0.00"))
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )

    # The staff member opens the form and types their own line.
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]+)"', page.text).group(1)

    # A colleague changes ONLY the food order underneath them.
    theirs = dict(document.content)
    theirs["food_order"] = {
        "line_items": [{"description": "Colleague platter", "quantity": 9, "unit_price": "250.00"}],
        "note": None,
    }
    documents_service.update_content(db, document, theirs, actor="staff:other")
    db.refresh(document)
    assert document.status == DocumentStatus.draft

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            "csrf_token": csrf,
            "content_expect": expect,
            "item_descriptions": ["MY OWN oyster station"],
            "item_quantities": ["7"],
            "item_unit_prices": ["180.00"],
            "item_categories": ["platter"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 409, "the food order moved and the save went through"
    table = response.text[response.text.index("Saving would put"):]
    assert "<strong>Food order</strong>" in table, "the refusal names nothing"
    assert "9 x Colleague platter @ 250" in table
    assert "7 x MY OWN oyster station @ 180" in table, "their own lines were not offered back"
    # And the form below it still holds what they typed, not the colleague's.
    assert "MY OWN oyster station" in response.text


# --- one parser, two strictnesses ---------------------------------------------


def _draft_with_a_platter(db, loft, name="Food Parse"):
    contact = Contact(name=name, email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 17),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    content = generate_beo_content(booking, [GRAZING], deposit_paid=Decimal("0.00"))
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )
    return booking, document


def _form_fields(admin_client, booking, document):
    import re

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert page.status_code == 200
    return {
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
        "content_expect": re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1),
    }


def test_a_half_typed_price_is_refused_and_changes_nothing(db, loft, admin_client):
    """The strict half of the shared parser, which had no test of its own.

    The 422 has to land BEFORE anything is written: the guest arrival time
    on the same form writes through to the Booking itself, so a parse that
    ran after it would leave the booking changed by a save that was
    refused.
    """
    booking, document = _draft_with_a_platter(db, loft, "Food Parse Bad Price")
    before_arrival = booking.guest_arrival_time
    before_food = dict(document.content["food_order"])

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            **_form_fields(admin_client, booking, document),
            "item_descriptions": ["Oyster station"],
            "item_quantities": ["7"],
            "item_unit_prices": ["180.0.0"],
            "guest_arrival_time": "17:30",
        },
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert "Oyster station" in response.json()["detail"]
    db.refresh(booking)
    db.refresh(document)
    assert booking.guest_arrival_time == before_arrival, "a refused save moved the booking"
    assert document.content["food_order"] == before_food


def test_a_fractional_quantity_is_refused_too(db, loft, admin_client):
    """int(), not Decimal(): half a platter is not a thing to put on a run
    sheet, and it would total wrongly against a per-item price."""
    booking, document = _draft_with_a_platter(db, loft, "Food Parse Half")

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            **_form_fields(admin_client, booking, document),
            "item_descriptions": ["Oyster station"],
            "item_quantities": ["1.5"],
            "item_unit_prices": ["180.00"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 422


def test_a_category_the_document_cannot_print_is_dropped(db, loft, admin_client):
    """The whitelist is what the Event Order has a heading for. Storing
    anything else would take the line off the page without a word."""
    booking, document = _draft_with_a_platter(db, loft, "Food Parse Category")

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            **_form_fields(admin_client, booking, document),
            "item_descriptions": ["Oyster station", "Sourdough"],
            "item_quantities": ["007", "2"],
            "item_unit_prices": ["180.00", "012.50"],
            "item_categories": ["banquet", "side"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    db.refresh(document)
    stored = {line["description"]: line for line in document.content["food_order"]["line_items"]}
    assert "category" not in stored["Oyster station"], "an unprintable category was stored"
    assert stored["Sourdough"]["category"] == "side"
    # And the strict parse stored money, not the strings the browser sent.
    # The values posted are deliberately shaped so that echoing the string
    # back would FAIL: "007" -> the int 7, "012.50" -> "12.50". The first
    # version of this assertion posted "12.00" and asserted "12.00", which
    # str(Decimal(...)) satisfies without parsing anything at all.
    assert stored["Oyster station"]["quantity"] == 7
    assert stored["Oyster station"]["quantity"] != "007"
    assert stored["Sourdough"]["unit_price"] == "12.50"


def test_a_price_that_is_not_a_number_never_reaches_a_total(db, loft, admin_client):
    """Decimal("nan") and Decimal("Infinity") both parse. Stored, they make
    compute_food_order_total answer NaN and put it on a client's Event Order
    and the invoice cut from it. This is the one protected field that is
    money, so the parse has to mean it."""
    booking, document = _draft_with_a_platter(db, loft, "Food Parse NaN")

    for bad in ("nan", "Infinity", "-Infinity"):
        response = admin_client.post(
            f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
            data={
                **_form_fields(admin_client, booking, document),
                "item_descriptions": ["Oyster station"],
                "item_quantities": ["7"],
                "item_unit_prices": [bad],
            },
            follow_redirects=False,
        )
        assert response.status_code == 422, f"{bad!r} was accepted as a price"


def test_a_refused_save_does_not_throw_the_typing_away_over_a_bad_price(db, loft, admin_client):
    """THE difference between strict=False and strict=True, which nothing in
    the suite distinguished: flipping the conflict path to strict=True left
    all 87 conflict and food tests green.

    A staff member whose save is refused because a colleague moved something
    must get their own words back -- including a half-typed price. Parsing
    strictly here would answer 422 and discard the lot, which is the failure
    the conflict screen exists to prevent, arriving by a different door."""
    booking, document = _draft_with_a_platter(db, loft, "Food Parse Lenient")
    form = _form_fields(admin_client, booking, document)

    # A colleague moves a written field, so the save will be refused.
    documents_service.update_content_fields(
        db, document, {"dietaries": "1x severe nut allergy (table 4)."},
        actor="staff:other", authored_fields=dr.PROTECTED_FIELD_NAMES,
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            **form,
            "item_descriptions": ["Oyster station"],
            "item_quantities": ["7"],
            "item_unit_prices": ["180.0.0"],   # half-typed, mid-sentence
        },
        follow_redirects=False,
    )

    assert response.status_code == 409, "a refusal turned into a 422 and threw their typing away"
    assert "Oyster station" in response.text, "their line did not come back"
    assert "180.0.0" in response.text, "their half-typed price did not come back"
