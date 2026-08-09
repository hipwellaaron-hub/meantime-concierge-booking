import datetime as dt
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSRANGE, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookingStatus(str, enum.Enum):
    enquiry = "enquiry"
    offered = "offered"
    tentative = "tentative"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"
    dead = "dead"


booking_status_enum = SAEnum(BookingStatus, name="booking_status", native_enum=True)

# Only these statuses actually hold the space -- everything else is
# excluded from the double-booking check below. This is deliberately a
# positive list (rather than "everything except cancelled/dead") because
# lead capture needs multiple enquiries/offers to coexist for the same
# date before one of them is confirmed: an 'enquiry' or an 'offered'
# booking is provisional and must not block someone else's enquiry for
# that slot from even being logged. Only once a booking becomes
# 'tentative' (a soft hold) or 'confirmed' does it actually reserve the
# space; 'completed' is included so past bookings still read correctly.
BLOCKING_STATUSES = (BookingStatus.tentative, BookingStatus.confirmed, BookingStatus.completed)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True)

    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Nullable: at enquiry stage the client often only knows roughly when
    # they want the event (see proposed_time_slot below), not exact times.
    # A NULL here makes time_range (and thus the exclusion constraint,
    # which range types treat NULL as "never conflicts") correctly not
    # reserve anything until real times are set.
    start_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    # Free-text, e.g. "Saturday evening" or "around 2pm" -- what iVvy calls
    # the proposed time slot, captured at enquiry stage before start_time/
    # end_time are pinned down.
    proposed_time_slot: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Drives the GIST exclusion constraint below. Deliberately tz-naive local
    # time, not tstzrange: Postgres STORED generated columns must be
    # IMMUTABLE, and `AT TIME ZONE <name>` is only STABLE (DST rules can
    # change), so it's rejected as a generation expression. Every booking is
    # entered and compared in the venue's own local clock, so naive time is
    # correct here and side-steps that restriction entirely.
    #
    # The explicit CASE matters: tsrange(NULL, NULL, '[)') does NOT return
    # SQL NULL, it returns the *unbounded* range (-infinity, infinity) --
    # which then conflicts with every other row in that space, including
    # itself. A real NULL is what the exclusion constraint treats as
    # "never conflicts", which is what an unknown time should mean (e.g.
    # a migrated booking with no time-of-day on record).
    time_range = mapped_column(
        TSRANGE,
        Computed(
            "CASE WHEN start_time IS NULL OR end_time IS NULL THEN NULL "
            "ELSE tsrange(event_date + start_time, event_date + end_time, '[)') END",
            persisted=True,
        ),
    )

    status: Mapped[BookingStatus] = mapped_column(
        booking_status_enum, nullable=False, default=BookingStatus.enquiry
    )
    event_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    adult_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    child_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reference_code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Captured at enquiry time so the ivvy.com.au marketplace's actual
    # contribution as a lead source is measurable before deciding whether
    # to cancel that listing -- see app/services/lead_analytics.py.
    lead_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lead_referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Provenance for bookings imported from a prior system (e.g. iVvy).
    # migration_external_ref is the source system's own booking code --
    # unique per source so re-running an import is idempotent, and useful
    # later for the parallel-run reconciliation report. migration_snapshot
    # keeps the source system's extra fields (company, coordinator, its
    # financial totals) verbatim for reference; it is NOT an authoritative
    # invoice/payment record -- see app/services/ivvy_import.py.
    migration_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    migration_external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    migration_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    space: Mapped["Space"] = relationship(back_populates="bookings")
    contact: Mapped["Contact"] = relationship(back_populates="bookings")
    events: Mapped[list["BookingEvent"]] = relationship(back_populates="booking", order_by="BookingEvent.created_at")
    documents: Mapped[list["Document"]] = relationship(back_populates="booking", order_by="Document.version")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="booking", order_by="Invoice.created_at")

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="ck_booking_end_after_start"),
        UniqueConstraint("migration_source", "migration_external_ref", name="uq_booking_migration_ref"),
        # Structural double-booking prevention: two bookings that actually
        # hold the space (tentative/confirmed/completed -- see
        # BLOCKING_STATUSES above) can never have overlapping time ranges
        # for the same space. Enforced by Postgres itself (GIST +
        # btree_gist), not application logic, so it holds even under
        # concurrent inserts. Enquiries and offers are deliberately NOT
        # blocking, so multiple leads for the same date can be logged
        # before one of them is confirmed.
        ExcludeConstraint(
            ("space_id", "="),
            ("time_range", "&&"),
            where="status IN ('tentative', 'confirmed', 'completed')",
            using="gist",
            name="excl_booking_space_time_overlap",
        ),
    )
