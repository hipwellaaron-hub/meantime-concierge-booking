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
