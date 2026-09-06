"""Scheduled sweep for server-side conversion dispatch.

Run as its own Railway cron service (every 15 minutes is plenty), the same
pattern as app/send_digest.py:

    python -m app.dispatch_conversions

Retries failed Meta Conversions API sends whose backoff has elapsed, sends
a first Meta copy for any enquiry whose background send never ran, and
sends the GA4 Measurement Protocol fallback for enquiries whose browser
never confirmed its own GA4 event after the grace period. Does nothing
unless TRACKING_SERVER_DISPATCH_ENABLED is true and the relevant secrets
are set, so it is safe to schedule on every environment.
"""

import logging

from app.database import SessionLocal
from app.services import conversions

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not conversions.server_dispatch_enabled():
        logger.info("Server-side conversion dispatch is disabled here; nothing to do.")
        return
    with SessionLocal() as db:
        summary = conversions.run_sweep(db)
    logger.info("Conversion sweep: %s", summary)


if __name__ == "__main__":
    main()
