"""A completed wizard has to reach Aaron. Before this, "escalated to
Aaron" wrote a line to the audit trail and stopped -- nothing actually
told him, inside the 14-day window where details stop being negotiable.
"""

import datetime as dt
from unittest.mock import patch

from app.models import Contact
from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services import notifications
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content


def _booking(db, space, name="Wizard Notify Test"):
    contact = Contact(name="Wizard Client", email=f"wiz.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 6, 12),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )


def test_subject_leads_with_the_reference(db, loft):
    booking = _booking(db, loft)
    subject = notifications.build_wizard_submission_subject(booking)
    assert subject == f"{booking.reference_code} has completed the Guided Booking Wizard"


def test_body_leads_with_outstanding_items_when_there_are_any(db, loft):
    booking = _booking(db, loft)
    body = notifications.build_wizard_submission_body(
        booking,
        outstanding_items=["Accessibility need raised against a non-accessible space"],
        dashboard_base_url="https://example.test",
    )
    assert body.startswith("NEEDS REVIEW (1 item(s))")
    assert "Accessibility need raised" in body
    assert "have NOT been sent" in body
    assert f"https://example.test/admin/bookings/{booking.id}" in body


def test_body_says_so_when_nothing_is_outstanding(db, loft):
    booking = _booking(db, loft)
    body = notifications.build_wizard_submission_body(
        booking, outstanding_items=[], dashboard_base_url="https://example.test"
    )
    assert "Submission was clean" in body
    assert "NEEDS REVIEW" not in body


def test_the_email_never_goes_to_the_client(db, loft):
    """The client gets the wizard's own confirmation screen. Nothing in
    this system emails them."""
    booking = _booking(db, loft)
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "venue@example.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "app-password"), \
         patch.object(notifications, "_send_via_gmail_smtp") as send:
        notifications.send_wizard_submission_email(
            booking, outstanding_items=[], dashboard_base_url="https://example.test"
        )
    message = send.call_args[0][0]
    assert message["To"] == notifications.ENQUIRY_NOTIFICATION_RECIPIENT
    assert booking.contact.email not in str(message)
    assert message["Reply-To"] is None  # not a message to reply to


# --- the BEO worklist the dashboard tile links to ----------------------------


def test_a_draft_beo_appears_in_the_review_worklist(db, loft, hamilton):
    booking = _booking(db, loft, name="Has Draft BEO")
    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    listed = documents_service.get_beos_awaiting_review(db, hamilton)
    assert booking.id in {d.booking_id for d in listed}


def test_sending_the_beo_clears_it_from_the_worklist(db, loft, hamilton):
    """Self-clearing, like every other worklist here -- nothing to tick off."""
    booking = _booking(db, loft, name="Sent BEO")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    assert booking.id in {d.booking_id for d in documents_service.get_beos_awaiting_review(db, hamilton)}

    documents_service.mark_sent(db, document, actor="test")
    assert booking.id not in {d.booking_id for d in documents_service.get_beos_awaiting_review(db, hamilton)}


def test_an_archived_bookings_beo_is_not_listed(db, loft, hamilton):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _booking(db, loft, name="Archived With BEO")
    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    change_status(db, booking, BookingStatus.archived, actor="test")
    assert booking.id not in {d.booking_id for d in documents_service.get_beos_awaiting_review(db, hamilton)}


def test_an_agreement_draft_is_not_a_beo_to_review(db, loft, hamilton):
    from app.services.document_generation import generate_agreement_content

    booking = _booking(db, loft, name="Agreement Only")
    documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    assert booking.id not in {d.booking_id for d in documents_service.get_beos_awaiting_review(db, hamilton)}


def test_dashboard_tile_count_matches_the_worklist(admin_client, db, loft, hamilton):
    for i in range(2):
        booking = _booking(db, loft, name=f"Tile BEO {i}")
        documents_service.create_new_version(
            db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
        )

    page = admin_client.get("/admin/")
    assert "BEOs to review" in page.text
    assert 'href="/admin/triage#beos-to-review"' in page.text

    triage = admin_client.get("/admin/triage")
    listed = documents_service.get_beos_awaiting_review(db, hamilton)
    assert f"BEOs to review ({len(listed)})" in triage.text


# --- the BEO must actually show what the client chose -------------------------


def test_a_wizard_generated_beo_renders_its_food_item_names(db, loft, hamilton, menu_items):
    """Regression: document.html read item["item"] while the wizard writes
    item["description"], so every wizard-generated BEO rendered a food
    table with prices and quantities but a blank Item column -- on the
    staff preview, the client's copy and the PDF alike."""
    from app.services.wizard_generation import build_food_line_items
    from app.templating import templates

    booking = _booking(db, loft, name="Food Names Shown")
    grazing = menu_items["Grazing Platter"]
    food_response = {
        "platters": [{"menu_item_id": str(grazing.id), "quantity": 2}],
        "pizzas": [],
    }
    line_items, outstanding = build_food_line_items(db, booking, food_response)
    assert outstanding == []
    assert line_items[0]["description"] == "Grazing Platter"

    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking, line_items), actor="test"
    )
    html = templates.get_template("document.html").render(
        document=document, booking=booking, is_staff_preview=True
    )
    assert "Grazing Platter" in html, "the client's own selections must appear on the BEO"


def test_a_beo_stored_with_the_older_item_key_still_renders(db, loft):
    """BEOs already saved before the key was standardised must not go
    blank."""
    from app.templating import templates

    booking = _booking(db, loft, name="Legacy Key")
    legacy = generate_beo_content(booking, [{"item": "Antipasto Platter", "quantity": 1, "unit_price": "100.00"}])
    document = documents_service.create_new_version(db, booking, DocumentType.beo, legacy, actor="test")
    html = templates.get_template("document.html").render(
        document=document, booking=booking, is_staff_preview=True
    )
    assert "Antipasto Platter" in html


def test_editing_a_beo_writes_the_description_key(admin_client, db, loft):
    """The edit form must write what the renderer and invoicing read, or
    a hand-edited BEO goes blank the moment it's saved."""
    import re

    booking = _booking(db, loft, name="Edit Writes Description")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    form = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            "csrf_token": csrf_token,
            "item_descriptions": ["Pork Belly Bites"],
            "item_quantities": ["3"],
            "item_unit_prices": ["100.00"],
            "catering_order_and_service_style": "", "bar_structure": "",
            "room_layout_notes": "", "music_entertainment": "", "special_notes": "",
        },
        follow_redirects=False,
    )
    db.refresh(document)
    assert document.content["food_order"]["line_items"][0]["description"] == "Pork Belly Bites"
