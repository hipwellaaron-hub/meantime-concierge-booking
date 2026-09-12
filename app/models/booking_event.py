import datetime as dt
import enum
import uuid

from sqlalchemy import BigInteger, DateTime, FetchedValue, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookingEventType(str, enum.Enum):
    created = "created"
    status_changed = "status_changed"
    field_changed = "field_changed"


class BookingEvent(Base):
    """Append-only audit log. Rows are never updated or deleted (enforced by
    a DB trigger, see the migration) — reconstructing how a booking got to
    its current state must never require re-reading email chains."""

    __tablename__ = "booking_events"
    # Every read of this table is per-booking and newest-first (the
    # relationship below, was_hand_edited, the timeline reads). Without it
    # there was no index at all beyond the primary key -- a Postgres FK
    # does not create one -- on the append-only log that every write in the
    # app adds to and nothing ever deletes from. Migration c1a7e4b90d52.
    __table_args__ = (
        Index("ix_booking_events_booking_created", "booking_id", "created_at"),
        Index("ix_booking_events_booking_seq", "booking_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # ORDER BY THIS, not created_at. `created_at` is server_default now(),
    # and Postgres now() is TRANSACTION START time -- so every event written
    # in one transaction shares a timestamp to the microsecond, and ordering
    # by it orders on tied values. Which row came first was then whatever
    # plan the planner chose: a sequential scan gives heap order (insertion
    # order, because this table is append-only), an index scan gives index
    # order, and the plan flips when the statistics change.
    #
    # It is not only a display problem. document_regeneration orders
    # created_at DESC to find the LATEST event of a kind; a tie there returns
    # an arbitrary one of several.
    #
    # BIGSERIAL, assigned at INSERT, so two rows in one transaction differ
    # (migration c5f8a1d3e720). Existing rows were filled by the ADD COLUMN
    # rewrite in heap order, which for an append-only table is the order they
    # were written -- so history keeps the ordering it already had.
    # FetchedValue, not a server_default naming the sequence: the column was
    # created as BIGSERIAL by the migration, so Postgres already owns its
    # default and SQLAlchemy only needs to know not to supply one and to read
    # the value back after INSERT. Spelling the nextval() out here would be
    # decorative -- it is never what fills the column -- and a mutation check
    # proved exactly that by replacing it with "0" and changing nothing.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=FetchedValue())

    booking: Mapped["Booking"] = relationship(back_populates="events")
