"""Integration point for the Claude-powered draft-and-approve email
pipeline described in the build brief. That pipeline does not exist yet
(confirmed at the start of this build) -- this is a marked seam for where
it plugs in once built, not a working implementation. It deliberately
does nothing today rather than fake a notification that was never sent.

Also: the staff notification digest (app.services.digest), which is a
real, working send -- unlike notify_new_enquiry below, DIGEST_API_KEY
configures a genuine outbound email via Resend's API. Failing loudly
when unconfigured beats silently pretending a digest went out (same
reasoning as app.services.stripe_integration.is_configured()).
"""

import os

import httpx

from app.models import Booking

RESEND_API_URL = "https://api.resend.com/emails"

DIGEST_API_KEY = os.environ.get("DIGEST_API_KEY")
DIGEST_FROM_EMAIL = os.environ.get("DIGEST_FROM_EMAIL")
DIGEST_RECIPIENT_EMAIL = os.environ.get("DIGEST_RECIPIENT_EMAIL")


class DigestEmailNotConfigured(RuntimeError):
    pass


def notify_new_enquiry(booking: Booking) -> None:
    """Called once a new enquiry is captured. Once the draft pipeline
    exists, this is where it gets triggered to read the booking's
    structured fields and produce a draft reply for human approval --
    per the cross-cutting rule, it should read structured state, not
    re-derive it from raw email threads."""
    return None


def is_digest_email_configured() -> bool:
    return bool(DIGEST_API_KEY and DIGEST_FROM_EMAIL and DIGEST_RECIPIENT_EMAIL)


def send_digest_email(subject: str, text_body: str) -> None:
    """Sends the staff digest via Resend's API. Raises
    DigestEmailNotConfigured if the required environment variables aren't
    set -- failing loudly beats silently skipping a digest that was
    never sent."""
    if not is_digest_email_configured():
        raise DigestEmailNotConfigured(
            "DIGEST_API_KEY, DIGEST_FROM_EMAIL, and DIGEST_RECIPIENT_EMAIL must all be set "
            "before the digest can actually send an email."
        )

    response = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {DIGEST_API_KEY}"},
        json={"from": DIGEST_FROM_EMAIL, "to": [DIGEST_RECIPIENT_EMAIL], "subject": subject, "text": text_body},
        timeout=15.0,
    )
    response.raise_for_status()
