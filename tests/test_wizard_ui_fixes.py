"""The 13-item wizard walkthrough brief: save-and-resume, setup access as
a request, music multi-select, dietary markers, review free text, bar-tab
merge semantics."""

import datetime as dt
from unittest.mock import patch

import pytest

from app.models import Contact
from app.services import wizard as wizard_service
from app.services.booking import create_booking
from app.services.wizard import CakeChoiceType, MusicType
from app.services.wizard_generation import build_bar_structure_text, build_music_text, build_special_notes


def _booking(db, space, name="UI Fix Test", event_date=dt.date(2027, 6, 12)):
    contact = Contact(name="ui fix client", email=f"uifix.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


# --- 1. save and come back later ----------------------------------------------


def test_save_for_later_returns_absolute_due_date_and_resume_link(admin_client, db, loft):
    booking = _booking(db, loft, name="Save Later", event_date=dt.date(2026, 10, 31))
    session = wizard_service.get_or_create_session(db, booking, actor="test")

    resp = admin_client.post(f"/w/{session.access_token}/save-for-later", json={})
    assert resp.status_code == 200
    data = resp.json()
    # 31 Oct minus 14 days = Saturday 17 October -- absolute, never relative.
    assert data["due_date_display"] == "Saturday 17 October"
    assert session.access_token in data["resume_url"]
    assert data["help_email"] == "meantimehamilton@gmail.com"
    # Gmail unconfigured in tests: the email fails but the panel data is
    # complete and the failure is on the audit trail, not swallowed silently.
    assert data["email_sent"] is False
    db.refresh(booking)
    events = {e.event_type for e in booking.events}
    assert "wizard_saved_for_later" in events
    assert "wizard_resume_email_failed" in events


def test_resume_email_goes_to_the_client_with_link_and_date(db, loft):
    from app.services import notifications

    booking = _booking(db, loft, name="Resume Email")
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "venue@example.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "pw"), \
         patch.object(notifications, "_send_via_gmail_smtp") as send:
        notifications.send_wizard_resume_email(
            booking, resume_url="https://example.test/w/tok", due_date_display="Saturday 17 October"
        )
    message = send.call_args[0][0]
    assert message["To"] == booking.contact.email
    body = message.get_content()
    assert "https://example.test/w/tok" in body
    assert "Please complete this by Saturday 17 October." in body
    assert "meantimehamilton@gmail.com" in body
    assert "Ui Fix Client" in body  # name display-cased even though stored lowercase


# --- 3. setup access is always a request --------------------------------------


def test_wizard_setup_access_is_always_a_request_even_at_standard_time(db, loft):
    booking = _booking(db, loft, name="Setup Request")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_basics_step(
        db, session, start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        food_service_time=dt.time(18, 30), setup_access_time=dt.time(14, 0),  # the standard time
        adult_count=40, child_count=0, actor="test",
    )
    db.refresh(booking)
    assert booking.setup_access_confirmed is False, "2:00pm is still a request, never auto-promised"

    # ...and the existing staff action confirms it.
    from app.services.booking import confirm_setup_access

    confirm_setup_access(db, booking, actor="staff:test")
    db.refresh(booking)
    assert booking.setup_access_confirmed is True


# --- 5. music multi-select ----------------------------------------------------


def test_music_multi_select_stores_all_types_and_renders_each(db, loft):
    booking = _booking(db, loft, name="Multi Music")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_music_step(
        db, session, music_types=[MusicType.own_playlist, MusicType.dj],
        notes="Playlist: Party Time", bump_in_notes=None, actor="test",
    )
    assert session.music_response["music_types"] == ["own_playlist", "dj"]
    text = build_music_text(session.music_response)
    assert "Spotify playlist" in text and "no links" in text
    assert "DJ." in text


def test_musician_option_renders_venue_arranged(db, loft):
    booking = _booking(db, loft, name="Musician")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_music_step(
        db, session, music_types=[MusicType.musician], notes=None, bump_in_notes=None, actor="test"
    )
    assert "Musician (venue-arranged)." in build_music_text(session.music_response)


def test_legacy_single_music_type_still_works(db, loft):
    booking = _booking(db, loft, name="Legacy Music")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_music_step(
        db, session, music_type=MusicType.dj, notes=None, bump_in_notes=None, actor="test"
    )
    assert session.music_response["music_types"] == ["dj"]
    # and a stored pre-multi-select response still composes:
    assert "DJ." in build_music_text({"music_type": "dj", "notes": None, "bump_in_notes": None})


def test_music_step_requires_at_least_one_selection(db, loft):
    booking = _booking(db, loft, name="No Music")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    with pytest.raises(ValueError, match="at least one"):
        wizard_service.save_music_step(db, session, music_types=[], notes=None, bump_in_notes=None, actor="test")


# --- 8. review free text ------------------------------------------------------


def test_final_notes_land_in_special_notes(db, loft):
    booking = _booking(db, loft, name="Final Notes")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None,
        accessibility_needs=None, additional_notes=None, actor="test",
    )
    session, _ = wizard_service.submit_review(
        db, session, actor="test", final_notes="Grandma needs a chair near the door"
    )
    assert session.extras_response["final_notes"] == "Grandma needs a chair near the door"
    notes = build_special_notes(session.extras_response, booking, [])
    assert "Client note: Grandma needs a chair near the door" in notes


# --- 12. bar tab merge semantics ----------------------------------------------


def test_bar_tab_and_hybrid_wording_matches_the_runout_choice():
    tab = build_bar_structure_text({"bar_structure": "bar_tab", "bar_limit": "2000", "bar_inclusions": ["beer"]})
    hybrid = build_bar_structure_text({"bar_structure": "hybrid", "bar_limit": "2000", "bar_inclusions": ["beer"]})
    # "come find me" -> host notified, no switch line
    assert "host notified" in tab
    assert "switches to guest-pays" not in tab
    # "guests buy their own" -> the switch line
    assert "switches to guest-pays" in hybrid


# --- 4. dietary markers -------------------------------------------------------


def test_markers_seeded_exactly_per_the_confirmed_website_table(db, menu_items):
    assert menu_items["Shoestring Fries"].dietary_markers == ["V", "DF"]
    assert menu_items["Crispy Popcorn Halloumi"].dietary_markers == ["V"]
    assert menu_items["Salt & Pepper Squid"].dietary_markers == ["DFA"]
    assert menu_items["Pork Belly Bites"].dietary_markers == ["DF"]
    assert menu_items["Margherita Pizza"].dietary_markers == ["V"]
    assert menu_items["Calabrese"].dietary_markers == []
    # Unconfirmed items stay NULL -- never guessed.
    assert menu_items["Chicken Tender Skewers"].dietary_markers is None
    assert menu_items["Grazing Platter"].dietary_markers is None
    assert menu_items["Vegetarian Pizza"].dietary_markers is None


def test_custom_vegan_platter_offered_at_140_marked_vegan(db, menu_items):
    from app.models.menu_item import MenuItemCategory

    item = menu_items["Custom Vegan Platter"]
    assert item.is_active is True
    assert item.category == MenuItemCategory.platter
    assert str(item.current_price) == "140.00"
    assert item.dietary_markers == ["VG"]
    assert item.contains_peanuts is False


def test_wizard_catalogue_payload_carries_markers(admin_client, db, loft, menu_items):
    booking = _booking(db, loft, name="Marker Payload")
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    page = admin_client.get(f"/w/{session.access_token}")
    compact = page.text.replace(" ", "")
    assert '"markers":["V","DF"]' in compact  # Shoestring Fries


# --- 11. guidance copy --------------------------------------------------------


def test_guidance_no_longer_undersells_platter_cost():
    from decimal import Decimal

    from app.services.food_guidance import generate_food_guidance

    guidance = generate_food_guidance(
        subtotal=Decimal("1300.00"), min_food_spend=Decimal("1000.00"),
        platter_count=7, total_guest_count=60,
    )
    assert "without much extra cost" not in guidance.message
    assert "cleared your minimum spend" in guidance.message
