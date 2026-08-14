"""Entry point for the staff notification digest. Meant to run on a
schedule (a separate cron-triggered Railway service pointed at this
module, not the always-on web service -- see the deploy notes wherever
this is wired up), but safe to run manually any time: it's pure read +
send, no state is written.

If DIGEST_GMAIL_ADDRESS/DIGEST_GMAIL_APP_PASSWORD/DIGEST_RECIPIENT_EMAIL
aren't set yet, prints the digest to stdout instead of failing the whole
run -- useful for a dry run against real data before the email side is
wired up, and means a misconfigured schedule doesn't need special-casing.

    python -m app.send_digest
"""

from app.config import settings
from app.database import SessionLocal
from app.models import Venue
from app.services import notifications
from app.services.digest import build_digest, render_digest_text


def main() -> None:
    db = SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug="hamilton").one()
        content = build_digest(db, venue)
        subject, body = render_digest_text(content, dashboard_base_url=settings.dashboard_base_url)

        if not notifications.is_digest_email_configured():
            print("DIGEST_GMAIL_ADDRESS/DIGEST_GMAIL_APP_PASSWORD/DIGEST_RECIPIENT_EMAIL not set -- printing instead of sending.")
            print(f"Subject: {subject}\n")
            print(body)
            return

        notifications.send_digest_email(subject, body)
        print(f"Digest sent: {subject}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
