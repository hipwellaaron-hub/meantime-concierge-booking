"""Lead-origin tracking.

Cancelling iVvy also drops the ivvy.com.au marketplace listing as a lead
source -- that's a real business decision with real revenue risk, and this
build does not make it. What it does do is capture enough to make that
decision with data: where a lead came from, so the marketplace's actual
contribution is measurable before anyone decides to cancel the contract.
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking

KNOWN_SOURCES = {"own_website", "google", "ivvy_marketplace", "referral", "other", "direct"}


def classify_lead_source(explicit_source: str | None, referrer: str | None) -> str:
    """Prefers an explicit source (e.g. a UTM parameter the embedding site
    captured and passed through) over guessing from the Referer header,
    which is easy to spoof or simply absent."""
    if explicit_source and explicit_source in KNOWN_SOURCES:
        return explicit_source

    if not referrer:
        return "direct"

    referrer_lower = referrer.lower()
    if "ivvy.com.au" in referrer_lower:
        return "ivvy_marketplace"
    if "google." in referrer_lower:
        return "google"
    if "meantime.com.au" in referrer_lower:
        return "own_website"
    return "referral"


def get_lead_source_breakdown(db: Session, since: dt.date | None = None) -> dict[str, int]:
    stmt = select(Booking.lead_source, func.count()).group_by(Booking.lead_source)
    if since is not None:
        stmt = stmt.where(Booking.created_at >= since)
    return {source or "unknown": count for source, count in db.execute(stmt).all()}
