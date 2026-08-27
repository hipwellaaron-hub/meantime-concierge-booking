"""The Event Order overhaul: dietaries that can't vanish, the cake priced
into the total, [REVIEW] never reaching clients, the Loft-only AV step,
vendor bump-ins as requests, and old stored BEOs still rendering.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.models import Contact
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.services import documents as documents_service
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking
from app.services.document_generation import (
    build_event_timeline,
    build_vendor_snapshot,
    format_day_date,
    generate_beo_content,
)
from app.services.wizard import BarStructure, CakeChoiceType, MusicType
from app.services.wizard_generation import (
    build_bar_structure_text,
    build_food_line_items,
    build_music_text,
)
from app.templating import templates


def _booking(db, space, *, name="BEO Overhaul Test", event_date=dt.date(2027, 5, 14),
             start_time=dt.time(18, 0), end_time=dt.time(23, 0)):
    contact = Contact(name="Overhaul Client", email=f"beo.{name.replace(' ', '.').lower()}@example.com", phone="0400 111 222")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=start_time, end_time=end_time, event_name=name,
        event_type="birthday", adult_count=42, child_count=6, notes=None, actor="test",
    )


def _render(document, booking, **kwargs):
    return templates.get_template("document.html").render(document=document, booking=booking, **kwargs)


# --- dietaries can never vanish -----------------------------------------------


def test_dietaries_key_always_present_with_default(db, booking):
    content = generate_beo_content(booking)
    assert content["dietaries"] == "No dietary requirements declared"
    assert generate_beo_content(booking, dietaries="2x dairy allergies")["dietaries"] == "2x dairy allergies"


def test_dietaries_section_renders_even_for_legacy_content_without_the_key(db, booking):
    """A BEO stored before the dietaries field existed must still show the
    section -- the floor team must always see the question was answered."""
    legacy_content = {
        "event_timeline": {"event_date": "2026-11-28", "start_time": "18:00", "end_time": "23:00", "notes": "old"},
        "catering_order_and_service_style": "Platters",
        "food_order": {"line_items": [{"item": "Grazing Platter", "quantity": 1, "unit_price": "250.00"}], "note": None},
        "total_food_spend": {"total": "250.00", "deposit_paid": None, "balance_due": None, "note": None},
        "bar_structure": "Cash bar", "room_layout_notes": "Cocktail",
        "music_entertainment": "DJ", "special_notes": "", "status": "confirmed",
        "_reference": {"reference_code": booking.reference_code, "event_name": booking.event_name,
                       "space_name": "The Loft", "adult_count": 40, "child_count": 0},
    }
    document = documents_service.create_new_version(db, booking, DocumentType.beo, legacy_content, actor="test")
    for surface in ({"is_staff_preview": True}, {}, {"is_pdf": True}):
        html = _render(document, booking, **surface)
        assert "No dietary requirements declared" in html


def test_old_stored_beo_renders_without_new_keys(db, booking):
    """The critical fallback guard: a pre-overhaul content dict through
    all three surfaces -- no crashes, and the food order still shows."""
    legacy_content = {
        "event_timeline": {"event_date": "2026-11-28", "start_time": "18:00", "end_time": "23:00",
                           "notes": "Setup access from 14:00 (confirmed)."},
        "catering_order_and_service_style": "10 platters ordered",
        "food_order": {"line_items": [{"item": "Antipasto", "quantity": 2, "unit_price": "100.00"}], "note": None},
        "total_food_spend": {"total": "200.00", "deposit_paid": "500.00", "balance_due": None, "note": None},
        "bar_structure": "Bar tab, capped at $2000.", "room_layout_notes": "Cocktail",
        "music_entertainment": "DJ. Bump-in 7pm.", "special_notes": "None.", "status": "confirmed",
        "_reference": {"reference_code": booking.reference_code, "event_name": booking.event_name,
                       "space_name": "The Loft", "adult_count": 40, "child_count": 0},
    }
    document = documents_service.create_new_version(db, booking, DocumentType.beo, legacy_content, actor="test")
    for surface in ({"is_staff_preview": True}, {}, {"is_pdf": True}):
        html = _render(document, booking, **surface)
        assert "Antipasto" in html
        assert "2x Antipasto: $200" in html  # line totals apply to old items too
        assert "DJ. Bump-in 7pm." in html  # legacy merged music field still shows under Music
        assert "The Loft" in html  # summary band space name


# --- [REVIEW] never reaches clients -------------------------------------------


def test_review_markers_never_reach_the_client_but_staff_see_them(db, booking):
    # A bare generate_beo_content carries [REVIEW] prompts in several fields.
    content = generate_beo_content(booking)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")

    staff_html = _render(document, booking, is_staff_preview=True)
    assert "[REVIEW]" in staff_html, "staff must still see the prompts"

    client_html = _render(document, booking)
    pdf_html = _render(document, booking, is_pdf=True)
    for html in (client_html, pdf_html):
        assert "[REVIEW]" not in html
        # the stray-None regression: a bare None rendered as element text
        assert ">None<" not in html


def test_internal_notes_visible_to_staff_only(db, booking):
    content = generate_beo_content(booking, internal_notes="Brief the floor team re noise.")
    document = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")

    assert "Brief the floor team" in _render(document, booking, is_staff_preview=True)
    assert "Brief the floor team" not in _render(document, booking)
    assert "Brief the floor team" not in _render(document, booking, is_pdf=True)


# --- cake becomes a priced line item ------------------------------------------


def test_in_house_cake_becomes_a_priced_dessert_line(db, loft, menu_items):
    booking = _booking(db, loft, name="Cake Priced")
    tiramisu = menu_items["Tiramisu Cake"]
    extras = {"cake_choice": {"type": "in_house", "menu_item_id": str(tiramisu.id), "notes": None}}
    line_items, outstanding = build_food_line_items(db, booking, {"platters": [], "pizzas": []}, extras)
    assert outstanding == []
    assert line_items == [
        {"description": "Tiramisu Cake — celebration cake", "quantity": 1, "unit_price": "80.00", "category": "dessert"}
    ]


def test_a_retired_layered_cake_on_an_existing_order_keeps_its_quoted_price(db, loft, menu_items):
    booking = _booking(db, loft, name="Retired Cake Honoured")
    retired = menu_items["Vanilla Cake (3 Layer)"]
    assert retired.is_active is False
    extras = {"cake_choice": {"type": "in_house", "menu_item_id": str(retired.id), "notes": None}}
    line_items, outstanding = build_food_line_items(db, booking, None, extras)
    assert outstanding == []
    assert line_items[0]["unit_price"] == "95.00"


def test_cake_flows_into_the_final_invoice_total(db, loft, menu_items):
    """The undercharge this whole fix exists for: the invoice must include
    the cake."""
    booking = _booking(db, loft, name="Cake Invoice", event_date=dt.date.today() + dt.timedelta(days=30))
    from app.models.booking import BookingStatus

    change_status(db, booking, BookingStatus.confirmed, actor="test")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]
    wizard_service.save_basics_step(
        db, session, start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        food_service_time=dt.time(18, 30), setup_access_time=dt.time(14, 0),
        adult_count=42, child_count=6, actor="test",
    )
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 1}], pizzas=[], actor="test"
    )
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="test"
    )
    wizard_service.save_music_step(db, session, music_type=MusicType.dj, notes=None, bump_in_notes=None, actor="test")
    wizard_service.save_vendors_step(db, session, vendors=[], actor="test")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.in_house,
        cake_menu_item_id=menu_items["Chocolate Mud Cake"].id, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements="1x vegan",
        accessibility_needs=None, additional_notes=None, actor="test",
    )
    wizard_service.save_av_step(db, session, video_slideshow=False, microphones_for_speeches=False, notes=None, actor="test")
    session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is True
    # Grazing $250 + Mud Cake $80
    assert result.invoice.subtotal == Decimal("330.00")
    descriptions = [li["description"] for li in result.document.content["food_order"]["line_items"]]
    assert "Chocolate Mud Cake — celebration cake" in descriptions
    assert result.document.content["dietaries"] == "1x vegan"


def test_outside_cake_adds_no_line_item(db, loft):
    booking = _booking(db, loft, name="Outside Cake")
    extras = {"cake_choice": {"type": "outside", "menu_item_id": None, "notes": "GF cupcakes"}}
    line_items, outstanding = build_food_line_items(db, booking, None, extras)
    assert line_items == []
    assert outstanding == []


def test_in_house_cake_without_a_selection_is_outstanding(db, loft):
    booking = _booking(db, loft, name="Cake Missing")
    extras = {"cake_choice": {"type": "in_house", "menu_item_id": None, "notes": None}}
    line_items, outstanding = build_food_line_items(db, booking, None, extras)
    assert line_items == []
    assert any("cake" in o.lower() for o in outstanding)


# --- bar text -----------------------------------------------------------------


def test_bar_tab_text_reads_cleanly_from_checkbox_inclusions():
    text = build_bar_structure_text({
        "bar_structure": "bar_tab", "bar_limit": "2000",
        "bar_inclusions": ["beer", "wine", "soft_drinks", "standard_spirits"],
    })
    assert "Bar tab to $2,000, covering beer, wine, soft drinks and standard spirits." in text
    assert "Cocktails and premium spirits purchased by guests." in text
    assert "package" not in text.lower()


def test_hybrid_is_a_cap_switch_never_a_category_split():
    text = build_bar_structure_text({"bar_structure": "hybrid", "bar_limit": "1500", "bar_inclusions": ["beer", "wine"]})
    assert "Once the cap is reached, the bar switches to guest-pays." in text
    assert "package" not in text.lower()


def test_legacy_free_text_inclusions_still_render():
    text = build_bar_structure_text({
        "bar_structure": "bar_tab", "bar_limit": "2000", "bar_inclusions": "beer and house wine",
    })
    assert "covering beer and house wine" in text


# --- timeline -----------------------------------------------------------------


def test_timeline_music_off_on_evening_but_not_daytime(db, loft):
    evening = _booking(db, loft, name="Evening Timeline")
    assert any("Music off by 11:30pm" in b for b in build_event_timeline(evening)["bullets"])

    daytime = _booking(db, loft, name="Daytime Timeline", event_date=dt.date(2027, 5, 12),
                       start_time=dt.time(11, 0), end_time=dt.time(15, 0))
    assert not any("Music off" in b for b in build_event_timeline(daytime)["bullets"])


def test_saturday_daytime_hard_stop_surfaces_on_the_timeline(db, loft):
    # 2027-05-15 is a Saturday.
    saturday = _booking(db, loft, name="Saturday Daytime", event_date=dt.date(2027, 5, 15),
                        start_time=dt.time(11, 0), end_time=dt.time(16, 0))
    assert any("concludes by 5:00pm (Saturday daytime)" in b for b in build_event_timeline(saturday)["bullets"])

    saturday_evening = _booking(db, loft, name="Saturday Evening", event_date=dt.date(2027, 5, 22),
                                start_time=dt.time(18, 0), end_time=dt.time(23, 0))
    assert not any("Saturday daytime" in b for b in build_event_timeline(saturday_evening)["bullets"])


def test_arrival_moments_and_pack_down_render_in_run_order(db, loft):
    booking = _booking(db, loft, name="Full Timeline")
    booking.setup_access_time = dt.time(14, 0)
    booking.setup_access_confirmed = True
    booking.guest_arrival_time = dt.time(18, 30)
    booking.food_service_time = dt.time(19, 0)
    booking.key_moments = [{"time": "20:30", "label": "Speeches"}]
    booking.pack_down_notes = "Decorator collects Sunday 10am"
    bullets = build_event_timeline(booking)["bullets"]
    assert "Setup access from 2:00pm (confirmed)" in bullets[0]
    assert "Guests arrive 6:30pm" in bullets
    assert "8:30pm — Speeches" in bullets
    assert any("Decorator collects Sunday 10am" in b for b in bullets)


# --- vendors: request vs confirmation -----------------------------------------


def test_vendor_bump_in_is_a_request_until_staff_confirm(db, loft):
    booking = _booking(db, loft, name="Vendor Request")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_vendors_step(
        db, session,
        vendors=[{"vendor_type": "dj", "name": "DJ Smooth", "contact_number": "0400", "bump_in_time": "16:00"}],
        actor="test",
    )
    db.refresh(booking)
    vendor = booking.vendors[0]
    assert vendor.bump_in_confirmed is False  # requested, pending
    snapshot = build_vendor_snapshot(booking.vendors)
    assert "requested — not yet confirmed" in snapshot[0]["bump_in_display"]
    assert any("DJ bump-in 4:00pm" in b for b in build_event_timeline(booking, snapshot)["bullets"])


def test_vendor_resave_preserves_confirmation_when_time_unchanged(db, loft):
    booking = _booking(db, loft, name="Vendor Resave")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    payload = [{"vendor_type": "band", "name": "The Tones", "contact_number": None, "bump_in_time": "15:00"}]
    wizard_service.save_vendors_step(db, session, vendors=payload, actor="test")
    db.refresh(booking)
    booking.vendors[0].bump_in_confirmed = True  # staff confirmed
    db.commit()

    wizard_service.save_vendors_step(db, session, vendors=payload, actor="test")
    db.refresh(booking)
    assert booking.vendors[0].bump_in_confirmed is True, "unchanged time must keep the confirmation"

    changed = [dict(payload[0], bump_in_time="17:00")]
    wizard_service.save_vendors_step(db, session, vendors=changed, actor="test")
    db.refresh(booking)
    assert booking.vendors[0].bump_in_confirmed is False, "a changed time is a NEW request"


def test_vendor_without_bump_in_lands_in_special_notes_with_contact(db, loft):
    from app.services.wizard_generation import build_special_notes

    booking = _booking(db, loft, name="Vendor No Bump")
    snapshot = [{"vendor_type": "photographer", "name": "Jane Lens", "contact_number": "0400 222 333",
                 "bump_in_time": None, "bump_in_confirmed": None, "bump_in_display": None}]
    notes = build_special_notes({}, booking, snapshot)
    assert "Photographer — Jane Lens (0400 222 333)" in notes


def test_confirm_bump_in_route_updates_a_draft_beo(admin_client, db, loft):
    import re

    booking = _booking(db, loft, name="Confirm Route")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_vendors_step(
        db, session,
        vendors=[{"vendor_type": "dj", "name": "DJ Confirm", "contact_number": None, "bump_in_time": "16:00"}],
        actor="test",
    )
    db.refresh(booking)
    vendor = booking.vendors[0]
    snapshot = build_vendor_snapshot(booking.vendors)
    content = generate_beo_content(booking, vendors=snapshot)
    document = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    assert "requested" in document.content["vendors"][0]["bump_in_display"]

    page = admin_client.get(f"/admin/bookings/{booking.id}")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/vendors/{vendor.id}/confirm-bump-in",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(vendor)
    db.refresh(document)
    assert vendor.bump_in_confirmed is True
    assert "confirmed" in document.content["vendors"][0]["bump_in_display"]
    assert "requested" not in document.content["vendors"][0]["bump_in_display"]


# --- AV: Loft only ------------------------------------------------------------


def test_av_step_only_in_the_loft_step_order(db, loft, mezzanine):
    from app.models.wizard_session import WizardStep

    loft_booking = _booking(db, loft, name="Loft Steps")
    mezz_booking = _booking(db, mezzanine, name="Mezz Steps")
    assert WizardStep.av in wizard_service.step_order_for(loft_booking)
    assert WizardStep.av not in wizard_service.step_order_for(mezz_booking)


def test_save_av_step_refused_for_non_loft(db, mezzanine):
    booking = _booking(db, mezzanine, name="Mezz AV Refused")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    with pytest.raises(ValueError, match="does not apply"):
        wizard_service.save_av_step(
            db, session, video_slideshow=True, microphones_for_speeches=False, notes=None, actor="test"
        )


def test_av_section_absent_from_document_for_non_loft(db, mezzanine):
    booking = _booking(db, mezzanine, name="Mezz No AV Section")
    content = generate_beo_content(booking)
    assert content["av"] is None
    document = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    html = _render(document, booking, is_staff_preview=True)
    assert "AV / Screen" not in html


def test_av_usb_deadline_is_an_absolute_weekday_date(db, loft):
    from app.services.document_generation import build_av_block

    booking = _booking(db, loft, name="USB Deadline", event_date=dt.date(2026, 8, 29))  # a Saturday
    block = build_av_block(booking, {"video_slideshow": True, "microphones_for_speeches": False, "notes": None})
    assert block["usb_deadline_display"] == "Thursday 27 August"
    assert format_day_date(dt.date(2026, 8, 27)) == "Thursday 27 August"


# --- music house rule ---------------------------------------------------------


def test_playlist_music_text_carries_the_name_only_rule():
    text = build_music_text({"music_type": "own_playlist", "notes": "Party Mix 2027", "bump_in_notes": None})
    assert "set to public" in text
    assert "no links" in text
