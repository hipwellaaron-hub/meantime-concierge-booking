import datetime as dt
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app.models import Booking, Contact
from app.services import notifications


def _booking(**overrides) -> Booking:
    contact = overrides.pop("contact", Contact(name="Jane Client", email="jane@example.com", phone="0400 000 000"))
    defaults = dict(
        event_name="Jane's 30th",
        event_type="Birthday",
        event_date=dt.date(2026, 9, 12),
        proposed_time_slot="Evening",
        adult_count=40,
        child_count=5,
        notes="Company: Acme Pty Ltd\nDates flexible: yes\nWanting balloons please.",
        reference_code="HAM-20260912-ABCDE",
    )
    defaults.update(overrides)
    booking = Booking(**defaults)
    booking.contact = contact
    return booking


def test_not_configured_by_default():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", None), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", None):
        assert notifications.is_digest_email_configured() is False
        assert notifications.is_gmail_smtp_configured() is False


def test_gmail_smtp_configured_does_not_require_digest_recipient():
    # The enquiry notification always sends to the fixed
    # ENQUIRY_NOTIFICATION_RECIPIENT -- it must not be gated on
    # DIGEST_RECIPIENT_EMAIL, which only the separate staff digest needs.
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", None):
        assert notifications.is_gmail_smtp_configured() is True
        assert notifications.is_digest_email_configured() is False


def test_send_raises_when_not_configured():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None):
        with pytest.raises(notifications.GmailSendNotConfigured):
            notifications.send_digest_email("Subject", "Body")


def test_digest_send_raises_when_recipient_missing():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", None):
        with pytest.raises(notifications.GmailSendNotConfigured):
            notifications.send_digest_email("Subject", "Body")


def test_send_logs_in_and_sends_via_gmail_smtp():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", "aaron@meantime.com.au"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp) as mock_smtp_ssl:
        notifications.send_digest_email("Test Subject", "Test Body")

    mock_smtp_ssl.assert_called_once_with(notifications.GMAIL_SMTP_HOST, notifications.GMAIL_SMTP_PORT, timeout=15.0)
    mock_smtp.login.assert_called_once_with("meantimehamilton@gmail.com", "fake-app-password")
    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["From"] == "meantimehamilton@gmail.com"
    assert sent_message["To"] == "aaron@meantime.com.au"
    assert sent_message["Subject"] == "Test Subject"
    assert sent_message.get_content().strip() == "Test Body"


def test_app_password_whitespace_is_stripped_including_non_breaking_space():
    # Real incident: Google displays the app password as 4 space-separated
    # groups; a copy from the page carried a non-breaking space (U+00A0)
    # rather than a plain one, which crashed smtplib's AUTH exchange.
    raw = "abcd\xa0efgh ijkl\tmnop"
    assert notifications._strip_all_whitespace(raw) == "abcdefghijklmnop"


def test_strip_all_whitespace_handles_none():
    assert notifications._strip_all_whitespace(None) is None


def test_send_raises_with_gmail_error_on_rejection():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "wrong-password"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", "aaron@meantime.com.au"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        with pytest.raises(notifications.GmailSendRejected, match="not accepted"):
            notifications.send_digest_email("Subject", "Body")


# --- enquiry notification email --------------------------------------------


def test_subject_is_scannable_with_event_date_and_guest_count():
    booking = _booking()
    subject = notifications.build_enquiry_notification_subject(booking)
    assert "Jane's 30th" in subject
    assert "12 Sep" in subject
    assert "45 guests" in subject


def test_subject_falls_back_to_tbd_for_missing_date_and_guests():
    booking = _booking(event_date=None, adult_count=0, child_count=0)
    subject = notifications.build_enquiry_notification_subject(booking)
    assert "date TBD" in subject
    assert "guest count TBD" in subject


def test_body_includes_every_captured_field():
    booking = _booking()
    body = notifications.build_enquiry_notification_body(
        booking, flags=["Event type 'Birthday' is unclear -- confirm what kind of event this actually is."],
        dashboard_base_url="https://example.test",
    )
    assert "HAM-20260912-ABCDE" in body
    assert "Jane Client" in body
    assert "jane@example.com" in body
    assert "0400 000 000" in body
    assert "Jane's 30th" in body
    assert "Birthday" in body
    assert "2026-09-12" in body
    assert "Evening" in body
    assert "45 total (40 adults, 5 children)" in body
    assert "Acme Pty Ltd" in body
    assert "Wanting balloons please." in body
    assert "confirm what kind of event this actually is" in body
    assert f"https://example.test/admin/bookings/{booking.id}" in body


def test_body_says_none_when_no_flags_raised():
    booking = _booking()
    body = notifications.build_enquiry_notification_body(booking, flags=[], dashboard_base_url="https://example.test")
    assert "FLAGS (0)" in body
    assert "None." in body


def test_body_handles_missing_contact():
    booking = _booking(contact=None)
    body = notifications.build_enquiry_notification_body(booking, flags=[], dashboard_base_url="https://example.test")
    assert "Not captured" in body


def test_send_enquiry_notification_sets_reply_to_client_and_internal_from():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    booking = _booking()

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_enquiry_notification_email(booking, flags=[], dashboard_base_url="https://example.test")

    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["To"] == "meantimehamilton@gmail.com"
    assert "meantimehamilton@gmail.com" in sent_message["From"]
    assert sent_message["Reply-To"] == "jane@example.com"
    assert "Jane's 30th" in sent_message["Subject"]


def test_send_enquiry_notification_omits_reply_to_when_no_valid_contact_email():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    booking = _booking(contact=None)

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_enquiry_notification_email(booking, flags=[], dashboard_base_url="https://example.test")

    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["Reply-To"] is None


def test_send_enquiry_notification_raises_when_not_configured():
    booking = _booking()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", None):
        with pytest.raises(notifications.GmailSendNotConfigured):
            notifications.send_enquiry_notification_email(booking, flags=[], dashboard_base_url="https://example.test")
