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
        notes="Company: Acme Pty Ltd\nDates flexible: yes",
        enquiry_text="Wanting balloons please.",
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


def test_subject_reads_as_a_message_to_the_client_not_an_internal_ticket():
    # Real incident, 2026-09-04: Chanai Duncombe's reply carried "Re: New
    # enquiry: ..." back to her, because Gmail keeps a thread's subject
    # verbatim. There is one subject now, and it has to work as both the
    # phone-scannable original AND the client-facing "Re:".
    booking = _booking()
    subject = notifications.build_enquiry_notification_subject(booking)
    assert subject == "Your enquiry — Jane's 30th, 12 September"
    assert "New enquiry" not in subject
    assert "guests" not in subject.lower()


def test_subject_omits_the_date_clause_when_no_date_given():
    booking = _booking(event_date=None)
    subject = notifications.build_enquiry_notification_subject(booking)
    assert subject == "Your enquiry — Jane's 30th"


def test_body_includes_every_client_describing_field():
    booking = _booking()
    body = notifications.build_enquiry_notification_body(booking)
    assert "HAM-20260912-ABCDE" in body
    assert "Jane Client" in body
    assert "jane@example.com" in body
    assert "0400 000 000" in body
    assert "Jane's 30th" in body
    assert "Birthday" in body
    assert "12-09-2026" in body  # day-first, per Aaron
    assert "Evening" in body
    assert "45 total (40 adults, 5 children)" in body
    assert "Acme Pty Ltd" in body
    assert "WHAT THEY WROTE" in body
    assert "Wanting balloons please." in body


def test_body_never_carries_staff_only_content():
    # The real leak, 2026-09-04: this body is quoted in FULL under Aaron's
    # reply, because Reply-To routes that reply to the client. Nothing
    # staff-only can be in it -- the admin link and the flags moved out
    # (link to a header, flags dropped -- they already live on Triage and
    # the booking page's own audit trail).
    booking = _booking()
    body = notifications.build_enquiry_notification_body(booking)
    assert "FLAGS" not in body
    assert "View in Concierge" not in body
    assert "/admin/bookings/" not in body


def test_body_omits_what_they_wrote_when_nothing_was_written():
    booking = _booking(enquiry_text=None)
    body = notifications.build_enquiry_notification_body(booking)
    assert "WHAT THEY WROTE" not in body


def test_body_handles_missing_contact():
    booking = _booking(contact=None)
    body = notifications.build_enquiry_notification_body(booking)
    assert "Not captured" in body


def test_send_enquiry_notification_sets_reply_to_client_and_internal_from():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    booking = _booking()

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://example.test")

    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["To"] == "meantimehamilton@gmail.com"
    assert "meantimehamilton@gmail.com" in sent_message["From"]
    assert sent_message["Reply-To"] == "jane@example.com"
    assert "Jane's 30th" in sent_message["Subject"]


def test_send_enquiry_notification_puts_the_admin_link_in_a_header_not_the_body():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    booking = _booking()

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://example.test")

    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["X-Concierge-Booking-Url"] == f"https://example.test/admin/bookings/{booking.id}"
    assert "/admin/bookings/" not in sent_message.get_content()


def test_send_enquiry_notification_omits_reply_to_when_no_valid_contact_email():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    booking = _booking(contact=None)

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://example.test")

    sent_message = mock_smtp.send_message.call_args.args[0]
    assert sent_message["Reply-To"] is None


def test_send_enquiry_notification_raises_when_not_configured():
    booking = _booking()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", None):
        with pytest.raises(notifications.GmailSendNotConfigured):
            notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://example.test")


# --- agreement-signed & deposit-paid alerts -----------------------------------

from decimal import Decimal  # noqa: E402

from app.models import Space  # noqa: E402


def _booking_with_space(**overrides) -> Booking:
    b = _booking(**overrides)
    b.space = Space(name="The Loft")
    return b


def test_agreement_signed_body_confirmed_headline():
    b = _booking_with_space()
    body = notifications.build_agreement_signed_body(
        b, signer_name="nicole jones", deposit_paid=True, now_confirmed=True,
        dashboard_base_url="https://x",
    )
    assert "CONFIRMED" in body
    assert "Nicole Jones" in body  # recased
    assert "The Loft" in body
    assert "HAM-20260912-ABCDE" in body
    assert "https://x/admin/bookings/" in body


def test_agreement_signed_body_waiting_on_deposit():
    b = _booking_with_space()
    body = notifications.build_agreement_signed_body(
        b, signer_name="Nicole Jones", deposit_paid=False, now_confirmed=False,
        dashboard_base_url="https://x",
    )
    assert "waiting on the deposit" in body.lower()
    assert "CONFIRMED" not in body


def test_deposit_paid_body_shows_amount_and_status():
    b = _booking_with_space()
    body = notifications.build_deposit_paid_body(
        b, amount=Decimal("500.00"), agreement_signed=False, now_confirmed=False,
        dashboard_base_url="https://x",
    )
    assert "$500.00" in body
    assert "waiting on the signed agreement" in body.lower()


def test_send_agreement_signed_goes_to_venue_no_reply_to():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    b = _booking_with_space()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_agreement_signed_email(
            b, signer_name="Nicole Jones", deposit_paid=False, now_confirmed=False,
            dashboard_base_url="https://x",
        )
    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["To"] == notifications.ENQUIRY_NOTIFICATION_RECIPIENT
    assert "Meantime Concierge" in msg["From"]
    assert msg["Reply-To"] is None  # an internal alert, not a message to answer
    assert "Agreement signed" in msg["Subject"]


def test_send_deposit_paid_goes_to_venue():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    b = _booking_with_space()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_deposit_paid_email(
            b, amount=Decimal("500.00"), agreement_signed=True, now_confirmed=True,
            dashboard_base_url="https://x",
        )
    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["To"] == notifications.ENQUIRY_NOTIFICATION_RECIPIENT
    assert "Deposit paid" in msg["Subject"]


def test_notify_agreement_signed_noops_when_not_configured():
    b = _booking_with_space()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None), \
         patch.object(notifications, "send_agreement_signed_email") as send:
        notifications.notify_agreement_signed(b, signer_name="X", deposit_paid=False, now_confirmed=False)
    send.assert_not_called()  # swallowed, no send, no raise


def test_notify_deposit_paid_swallows_send_failure():
    b = _booking_with_space()
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications, "send_deposit_paid_email", side_effect=RuntimeError("boom")):
        # Must not raise.
        notifications.notify_deposit_paid(b, amount=Decimal("500.00"), agreement_signed=False, now_confirmed=False)


# --- floor welcome email ------------------------------------------------------


def test_floor_welcome_body_has_setup_and_usage_no_password():
    body = notifications.build_floor_welcome_body(
        name="sally hipwell", email="sally@meantime.com.au",
        floor_url="https://book.example/floor", help_email="help@meantime.com.au",
    )
    assert "Sally Hipwell" in body  # recased
    assert "https://book.example/floor" in body
    assert "sally@meantime.com.au" in body
    assert "Add to Home Screen" in body  # iPhone install step
    assert "PAID" in body and "OWING" in body  # usage
    assert "password" in body.lower()  # mentions it -- but only that the manager gives it
    assert "Your manager will give you your password." in body


def test_send_floor_welcome_goes_to_the_new_user():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        notifications.send_floor_welcome_email(
            name="Sally", email="sally@meantime.com.au", floor_url="https://x/floor"
        )
    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["To"] == "sally@meantime.com.au"
    assert msg["Subject"] == "Your Meantime Floor access"


def test_notify_floor_welcome_returns_false_when_not_configured():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None):
        assert notifications.notify_floor_welcome(name="Sally", email="sally@meantime.com.au") is False


def test_notify_floor_welcome_returns_true_on_send():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake-app-password"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        assert notifications.notify_floor_welcome(name="Sally", email="sally@meantime.com.au") is True


# --- UI filters ---------------------------------------------------------------

def test_status_badge_and_time_ago_filters():
    from app import templating

    assert templating.status_badge("confirmed") == "green"
    assert templating.status_badge("paid") == "green"
    assert templating.status_badge("cancelled") == "wine"
    assert templating.status_badge("tentative") == "gold"
    assert templating.status_badge("enquiry") == ""
    assert templating.status_badge("something-unknown") == ""
    assert templating.status_badge(None) == ""

    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    assert templating.time_ago(now - _dt.timedelta(minutes=5)) == "5m ago"
    assert templating.time_ago(now - _dt.timedelta(hours=3)) == "3h ago"
    assert templating.time_ago(now - _dt.timedelta(days=2)) == "2d ago"
    assert templating.time_ago(now - _dt.timedelta(seconds=10)) == "just now"
    assert templating.time_ago(None) == ""
