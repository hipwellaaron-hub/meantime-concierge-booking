import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app.services import notifications


def test_not_configured_by_default():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", None), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", None):
        assert notifications.is_digest_email_configured() is False


def test_send_raises_when_not_configured():
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", None):
        with pytest.raises(notifications.DigestEmailNotConfigured):
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


def test_send_raises_with_gmail_error_on_rejection():
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", "meantimehamilton@gmail.com"), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "wrong-password"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", "aaron@meantime.com.au"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock_smtp):
        with pytest.raises(notifications.DigestEmailRejected, match="not accepted"):
            notifications.send_digest_email("Subject", "Body")
