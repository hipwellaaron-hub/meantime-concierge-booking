"""Integration point for the Claude-powered draft-and-approve email
pipeline described in the build brief. That pipeline does not exist yet
(confirmed at the start of this build) -- this is a marked seam for where
it plugs in once built, not a working implementation. It deliberately
does nothing today rather than fake a notification that was never sent.

Also: the staff notification digest (app.services.digest), which is a
real, working send -- unlike notify_new_enquiry below, DIGEST_GMAIL_*
configures a genuine outbound email sent via Gmail's own SMTP, so it
actually arrives from meantimehamilton@gmail.com (a third-party sender
like Resend can never send "from" a Gmail address -- only Google's own
servers, authenticated as that real account, can). Failing loudly when
unconfigured beats silently pretending a digest went out (same reasoning
as app.services.stripe_integration.is_configured()).
"""

import os
import smtplib
from email.message import EmailMessage

from app.models import Booking

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465

DIGEST_GMAIL_ADDRESS = os.environ.get("DIGEST_GMAIL_ADDRESS")
DIGEST_GMAIL_APP_PASSWORD = os.environ.get("DIGEST_GMAIL_APP_PASSWORD")
DIGEST_RECIPIENT_EMAIL = os.environ.get("DIGEST_RECIPIENT_EMAIL")


class DigestEmailNotConfigured(RuntimeError):
    pass


class DigestEmailRejected(RuntimeError):
    pass


def notify_new_enquiry(booking: Booking) -> None:
    """Called once a new enquiry is captured. Once the draft pipeline
    exists, this is where it gets triggered to read the booking's
    structured fields and produce a draft reply for human approval --
    per the cross-cutting rule, it should read structured state, not
    re-derive it from raw email threads."""
    return None


def is_digest_email_configured() -> bool:
    return bool(DIGEST_GMAIL_ADDRESS and DIGEST_GMAIL_APP_PASSWORD and DIGEST_RECIPIENT_EMAIL)


def send_digest_email(subject: str, text_body: str) -> None:
    """Sends the staff digest via Gmail's own SMTP, authenticated with an
    app password -- not a third-party API, so the email genuinely arrives
    from DIGEST_GMAIL_ADDRESS. Raises DigestEmailNotConfigured if the
    required environment variables aren't set -- failing loudly beats
    silently skipping a digest that was never sent."""
    if not is_digest_email_configured():
        raise DigestEmailNotConfigured(
            "DIGEST_GMAIL_ADDRESS, DIGEST_GMAIL_APP_PASSWORD, and DIGEST_RECIPIENT_EMAIL must all be "
            "set before the digest can actually send an email."
        )

    message = EmailMessage()
    message["From"] = DIGEST_GMAIL_ADDRESS
    message["To"] = DIGEST_RECIPIENT_EMAIL
    message["Subject"] = subject
    message.set_content(text_body)

    try:
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=15.0) as smtp:
            smtp.login(DIGEST_GMAIL_ADDRESS, DIGEST_GMAIL_APP_PASSWORD)
            smtp.send_message(message)
    except smtplib.SMTPException as exc:
        # Surfaces Gmail's actual rejection reason (bad app password,
        # account not enrolled in 2-step verification, etc.) rather than
        # a generic failure -- same reasoning as the Resend error body
        # that used to be swallowed here.
        raise DigestEmailRejected(f"Gmail rejected the digest email: {exc}") from exc
