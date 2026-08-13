from unittest.mock import patch

import pytest

from app.services import notifications


def test_not_configured_by_default():
    with patch.object(notifications, "DIGEST_API_KEY", None), \
         patch.object(notifications, "DIGEST_FROM_EMAIL", None), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", None):
        assert notifications.is_digest_email_configured() is False


def test_send_raises_when_not_configured():
    with patch.object(notifications, "DIGEST_API_KEY", None):
        with pytest.raises(notifications.DigestEmailNotConfigured):
            notifications.send_digest_email("Subject", "Body")


def test_send_posts_to_resend_with_expected_payload():
    class FakeResponse:
        def raise_for_status(self):
            return None

    with patch.object(notifications, "DIGEST_API_KEY", "re_fake_key"), \
         patch.object(notifications, "DIGEST_FROM_EMAIL", "digest@meantime.com.au"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", "aaron@meantime.com.au"), \
         patch.object(notifications.httpx, "post", return_value=FakeResponse()) as mock_post:
        notifications.send_digest_email("Test Subject", "Test Body")

    args, kwargs = mock_post.call_args
    assert args[0] == notifications.RESEND_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer re_fake_key"
    assert kwargs["json"] == {
        "from": "digest@meantime.com.au",
        "to": ["aaron@meantime.com.au"],
        "subject": "Test Subject",
        "text": "Test Body",
    }


def test_send_propagates_http_errors():
    class FakeResponse:
        def raise_for_status(self):
            raise Exception("Resend said no")

    with patch.object(notifications, "DIGEST_API_KEY", "re_fake_key"), \
         patch.object(notifications, "DIGEST_FROM_EMAIL", "digest@meantime.com.au"), \
         patch.object(notifications, "DIGEST_RECIPIENT_EMAIL", "aaron@meantime.com.au"), \
         patch.object(notifications.httpx, "post", return_value=FakeResponse()):
        with pytest.raises(Exception, match="Resend said no"):
            notifications.send_digest_email("Subject", "Body")
