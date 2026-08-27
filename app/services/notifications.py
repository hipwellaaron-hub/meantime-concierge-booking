"""Outbound email this app actually sends, all via Gmail's own SMTP
(authenticated as DIGEST_GMAIL_ADDRESS with an app password) rather than a
third-party API -- a third-party sender can never send "from" a Gmail
address, only Google's own servers, authenticated as that real account,
can. Two independent sends share this one mechanism:

- send_enquiry_notification_email: fires once per new enquiry (see
  app.services.enquiry_classification.notify_new_enquiry), the primary way
  Aaron finds out a lead exists and the only way an enquiry becomes
  visible to whatever drafts a client reply -- that pipeline only has
  Gmail access, not this database, so an enquiry that never lands in the
  inbox is invisible to it.
- send_digest_email: the periodic staff digest (app.services.digest),
  covering things that build up over time rather than needing an
  immediate ping.

Both fail loudly (raise) rather than silently skipping a send that never
happened -- same reasoning as app.services.stripe_integration.is_configured().
Retrying and recording the outcome is each caller's job, not this
module's: see app.services.enquiry_classification.notify_new_enquiry for
the enquiry side.
"""

import os
import smtplib
from email.message import EmailMessage

from app.models import Booking
from app.services import policy
from app.utils import is_valid_email

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465


def _strip_all_whitespace(value: str | None) -> str | None:
    # Google displays an app password as 4 space-separated groups for
    # readability; copying it from the page (or pasting through some
    # clipboard managers) can carry a non-breaking space (U+00A0) rather
    # than a plain ASCII one, which smtplib's AUTH exchange then fails to
    # base64-encode. The password works identically with or without the
    # spaces, so stripping every whitespace character -- not just leading/
    # trailing -- makes this immune to exactly how it was pasted in.
    if value is None:
        return None
    return "".join(ch for ch in value if not ch.isspace()) or None


DIGEST_GMAIL_ADDRESS = os.environ.get("DIGEST_GMAIL_ADDRESS")
DIGEST_GMAIL_APP_PASSWORD = _strip_all_whitespace(os.environ.get("DIGEST_GMAIL_APP_PASSWORD"))
DIGEST_RECIPIENT_EMAIL = os.environ.get("DIGEST_RECIPIENT_EMAIL")

# Fixed and independent of DIGEST_RECIPIENT_EMAIL on purpose: the brief is
# explicit that the enquiry notification goes to the venue's own inbox,
# not wherever the separate staff digest happens to be configured to go
# (DIGEST_RECIPIENT_EMAIL has been pointed at Aaron's personal address in
# at least one real config -- see tests/test_notifications.py).
ENQUIRY_NOTIFICATION_RECIPIENT = policy.VENUE_CONTACT_EMAIL


class GmailSendNotConfigured(RuntimeError):
    pass


class GmailSendRejected(RuntimeError):
    pass


def is_gmail_smtp_configured() -> bool:
    """The credential gate shared by every email this module sends --
    DIGEST_RECIPIENT_EMAIL is a separate, digest-only requirement (see
    is_digest_email_configured); the enquiry notification always sends to
    the fixed ENQUIRY_NOTIFICATION_RECIPIENT above and needs nothing else."""
    return bool(DIGEST_GMAIL_ADDRESS and DIGEST_GMAIL_APP_PASSWORD)


def is_digest_email_configured() -> bool:
    return is_gmail_smtp_configured() and bool(DIGEST_RECIPIENT_EMAIL)


def _send_via_gmail_smtp(message: EmailMessage) -> None:
    """The one place this module actually talks to Gmail. Raises
    GmailSendNotConfigured if the shared credentials aren't set,
    GmailSendRejected if Gmail itself refuses the send -- surfacing
    Gmail's real rejection reason (bad app password, account not
    enrolled in 2-step verification, etc.) rather than a generic
    failure."""
    if not is_gmail_smtp_configured():
        raise GmailSendNotConfigured(
            "DIGEST_GMAIL_ADDRESS and DIGEST_GMAIL_APP_PASSWORD must both be set before any email can "
            "actually send."
        )
    try:
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=15.0) as smtp:
            smtp.login(DIGEST_GMAIL_ADDRESS, DIGEST_GMAIL_APP_PASSWORD)
            smtp.send_message(message)
    except smtplib.SMTPException as exc:
        raise GmailSendRejected(f"Gmail rejected the email: {exc}") from exc


def send_digest_email(subject: str, text_body: str) -> None:
    """Sends the staff digest via Gmail's own SMTP. Raises
    GmailSendNotConfigured if the shared credentials aren't set, or if
    DIGEST_RECIPIENT_EMAIL specifically isn't -- failing loudly beats
    silently skipping a digest that was never sent."""
    if not DIGEST_RECIPIENT_EMAIL:
        raise GmailSendNotConfigured("DIGEST_RECIPIENT_EMAIL must be set before the digest can send.")

    message = EmailMessage()
    message["From"] = DIGEST_GMAIL_ADDRESS
    message["To"] = DIGEST_RECIPIENT_EMAIL
    message["Subject"] = subject
    message.set_content(text_body)
    _send_via_gmail_smtp(message)


def build_enquiry_notification_subject(booking: Booking) -> str:
    """Scannable in a phone push notification -- Aaron triages from this
    line alone before opening anything, so event name / date / guest
    count have to be visible without opening the message."""
    date_str = booking.event_date.strftime("%d %b") if booking.event_date else "date TBD"
    guest_count = booking.adult_count + booking.child_count
    guest_str = f"{guest_count} guests" if guest_count else "guest count TBD"
    return f"New enquiry: {booking.event_name} — {date_str} — {guest_str}"


def build_enquiry_notification_body(booking: Booking, *, flags: list[str], dashboard_base_url: str) -> str:
    """Plain text, not HTML -- read on a phone, and this is a working
    document (what a reply gets drafted from), not a marketing email that
    needs to look polished. Every field captured on the enquiry form is
    included verbatim; nothing here is summarized or dropped, since
    whatever isn't in this email doesn't exist for whoever drafts the
    reply (see the module docstring)."""
    contact = booking.contact
    guest_total = booking.adult_count + booking.child_count

    lines = [
        f"Reference: {booking.reference_code}",
        "",
        "CONTACT",
        f"Name: {contact.name if contact else 'Not captured'}",
        f"Email: {contact.email if contact else 'Not captured'}",
        f"Phone: {contact.phone if contact and contact.phone else 'Not given'}",
        "",
        "EVENT",
        f"Event name: {booking.event_name}",
        f"Type: {booking.event_type or 'Not given'}",
        f"Date: {booking.event_date.isoformat() if booking.event_date else 'Not given'}",
        f"Proposed time: {booking.proposed_time_slot or 'Not given'}",
        f"Guests: {guest_total} total ({booking.adult_count} adults, {booking.child_count} children)",
    ]

    if booking.notes:
        lines += ["", "NOTES", booking.notes]

    lines += ["", f"FLAGS ({len(flags)})"]
    if flags:
        lines += [f"- {f}" for f in flags]
    else:
        lines.append("None.")

    lines += ["", f"View in Concierge: {dashboard_base_url}/admin/bookings/{booking.id}"]

    return "\n".join(lines)


def send_enquiry_notification_email(booking: Booking, *, flags: list[str], dashboard_base_url: str) -> None:
    """One email per enquiry, sent to the venue's own inbox
    (ENQUIRY_NOTIFICATION_RECIPIENT) -- never to the client, who already
    has the thank-you page. From carries a display name that reads as
    internal so it's obviously not a message from the client; Reply-To is
    the client's own address (when there is a valid one on file) so
    hitting Reply in Gmail addresses the client directly rather than the
    system that sent this.

    Single attempt -- raises GmailSendNotConfigured / GmailSendRejected on
    failure. The caller (app.services.enquiry_classification.
    notify_new_enquiry) owns retrying and recording the outcome; this
    function only knows how to format and send one message."""
    contact = booking.contact

    message = EmailMessage()
    message["From"] = f"Meantime Concierge <{DIGEST_GMAIL_ADDRESS}>"
    message["To"] = ENQUIRY_NOTIFICATION_RECIPIENT
    if contact is not None and is_valid_email(contact.email):
        message["Reply-To"] = contact.email
    message["Subject"] = build_enquiry_notification_subject(booking)
    message.set_content(build_enquiry_notification_body(booking, flags=flags, dashboard_base_url=dashboard_base_url))

    _send_via_gmail_smtp(message)


def build_wizard_submission_subject(booking: Booking) -> str:
    """Leads with the reference so it's identifiable straight from a phone
    lock screen, matching how the enquiry subject is built."""
    return f"{booking.reference_code} has completed the Guided Booking Wizard"


def build_wizard_submission_body(
    booking: Booking, *, outstanding_items: list[str], dashboard_base_url: str
) -> str:
    """Plain text, same as the enquiry notification and for the same
    reason: read on a phone, and it's a working document rather than
    anything polished. Leads with whether the submission needs a human,
    because that's the only thing that changes what Aaron does next."""
    date_str = booking.event_date.isoformat() if booking.event_date else "Not set"
    guest_total = booking.adult_count + booking.child_count

    if outstanding_items:
        lines = [
            f"NEEDS REVIEW ({len(outstanding_items)} item(s))",
            "",
            "The BEO and invoice were prepared as drafts and have NOT been sent:",
        ]
        lines += [f"- {item}" for item in outstanding_items]
    else:
        lines = ["Submission was clean -- nothing outstanding."]

    lines += [
        "",
        "BOOKING",
        f"Reference: {booking.reference_code}",
        f"Event: {booking.event_name}",
        f"Date: {date_str}",
        f"Space: {booking.space.name}",
        f"Guests: {guest_total} total ({booking.adult_count} adults, {booking.child_count} children)",
        "",
        f"Review the BEO: {dashboard_base_url}/admin/bookings/{booking.id}",
    ]
    return "\n".join(lines)


def send_wizard_submission_email(
    booking: Booking, *, outstanding_items: list[str], dashboard_base_url: str
) -> None:
    """Fires when a client finishes the Guided Booking Wizard. Goes to the
    venue's own inbox only -- the client gets the wizard's own confirmation
    screen and is never emailed by this system.

    No Reply-To to the client here, unlike the enquiry notification: this
    isn't a message to reply to, it's a prompt to go and review a BEO.

    Single attempt, raises on failure. The caller
    (app.services.wizard_generation) owns swallowing that so a failed
    notification can never lose a client's completed submission."""
    message = EmailMessage()
    message["From"] = f"Meantime Concierge <{DIGEST_GMAIL_ADDRESS}>"
    message["To"] = ENQUIRY_NOTIFICATION_RECIPIENT
    message["Subject"] = build_wizard_submission_subject(booking)
    message.set_content(
        build_wizard_submission_body(
            booking, outstanding_items=outstanding_items, dashboard_base_url=dashboard_base_url
        )
    )
    _send_via_gmail_smtp(message)
