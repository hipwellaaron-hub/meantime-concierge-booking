"""One row per (enquiry, platform, channel): the durable record of every
attempt to tell an ad platform that a function enquiry happened.

Two channels exist for the same logical event and are recorded
separately because they have different failure modes:

- browser: the thank-you page ran gtag/fbq and beaconed back. The beacon
  is the only evidence; the platform's receipt is never visible to us.
- server: Concierge itself POSTed to the platform (Meta Conversions API,
  or the GA4 Measurement Protocol as a late fallback). The response is
  recorded so a failure can be retried and a success reconciled.

Status is per row. "accepted" means the provider answered with a receipt
(Meta returns events_received); "sent" means the request was accepted at
the transport level but the provider does not confirm ingestion (GA4's
Measurement Protocol always answers 204); "failed" carries the error and
a next_attempt_at for the retry sweep; "skipped" records a deliberate
non-send with its reason (no client id, dispatch disabled, browser
already confirmed) so reconciliation can tell "never tried" from "tried
and could not".

Nothing here is a secret and nothing here is personal data: the event id
is the booking reference, the receipt is the provider's trace id.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

PLATFORM_GA4 = "ga4"
PLATFORM_META = "meta"
PLATFORMS = (PLATFORM_GA4, PLATFORM_META)

CHANNEL_BROWSER = "browser"
CHANNEL_SERVER = "server"

STATUS_SENT = "sent"
STATUS_ACCEPTED = "accepted"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Never retry forever: a token that is wrong for a week is a human's job.
MAX_ATTEMPTS = 8


class ConversionDispatch(Base):
    __tablename__ = "conversion_dispatches"
    __table_args__ = (
        UniqueConstraint("booking_id", "platform", "channel", name="uq_conversion_dispatch_booking_platform_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(10), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    # The event id every copy of this conversion carries: the booking's
    # reference code. Meta deduplicates browser and server copies on it;
    # GA4 does not, which is why the server GA4 send is a fallback only.
    event_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    booking = relationship("Booking", back_populates="conversion_dispatches")
