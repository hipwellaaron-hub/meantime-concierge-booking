"""Entry point for the staff notification digest. Meant to run on a
schedule (a separate cron-triggered Railway service pointed at this
module, not the always-on web service -- see the deploy notes wherever
this is wired up), but safe to run manually any time: it's pure read +
send, no state is written.

EVERY VENUE, grouped by who receives it. Until 2026-09-12 this looked up
`slug="hamilton"` and sent one venue's digest, which would have left a
second company's overdue invoices and pending wizards invisible -- the exact
shape of Aaron's standing rule that jobs loop venues.

Venues are grouped BY RECIPIENT rather than sent one email each. Aaron,
2026-09-12: "one email with the venues sectioned, since I read it on a phone
first thing and two emails means one gets skimmed." Both venues point at the
same address today, so it is one email with a section each; the day one
venue's digest should go somewhere else, `venues.digest_recipient_email`
splits them with no code change here.

If DIGEST_GMAIL_ADDRESS/DIGEST_GMAIL_APP_PASSWORD/DIGEST_RECIPIENT_EMAIL
aren't set yet, prints the digest to stdout instead of failing the whole
run -- useful for a dry run against real data before the email side is
wired up, and means a misconfigured schedule doesn't need special-casing.

    python -m app.send_digest
"""

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Venue
from app.services import notifications
from app.services.digest import build_digest, render_combined_digest


def _group_by_recipient(db) -> dict[str | None, list[Venue]]:
    """Which venues share an inbox.

    The key is the venue's own `digest_recipient_email`, or None meaning
    "whatever DIGEST_RECIPIENT_EMAIL says" -- which is every venue today and
    is why today's result is a single group, hence a single email.

    Venues are ordered by name so the sections come out in a stable order:
    a digest whose sections shuffle between mornings is one you have to read
    rather than scan.
    """
    groups: dict[str | None, list[Venue]] = {}
    for venue in db.scalars(select(Venue).order_by(Venue.name)).all():
        groups.setdefault(venue.digest_recipient_email, []).append(venue)
    return groups


def main() -> None:
    db = SessionLocal()
    try:
        groups = _group_by_recipient(db)
        if not groups:
            # Not "all clear". A database with no venue cannot have been
            # checked, and a silent success here is how that stays invisible.
            print("No venues found -- nothing was checked. This is a fault, not an empty digest.")
            return

        configured = notifications.is_digest_email_configured()
        if not configured:
            print(
                "DIGEST_GMAIL_ADDRESS/DIGEST_GMAIL_APP_PASSWORD/DIGEST_RECIPIENT_EMAIL "
                "not set -- printing instead of sending."
            )

        for recipient, venues in groups.items():
            per_venue = [(venue, build_digest(db, venue)) for venue in venues]
            subject, body = render_combined_digest(
                per_venue, dashboard_base_url=settings.dashboard_base_url
            )

            if not configured:
                print(f"\n--- to: {recipient or 'DIGEST_RECIPIENT_EMAIL'} ---")
                print(f"Subject: {subject}\n")
                print(body)
                continue

            notifications.send_digest_email(subject, body, recipient=recipient)
            covered = ", ".join(v.trading_name or v.name for v in venues)
            print(f"Digest sent to {recipient or 'DIGEST_RECIPIENT_EMAIL'} covering {covered}: {subject}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
