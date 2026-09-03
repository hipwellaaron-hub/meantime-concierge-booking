import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
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
    # Distinct from `cancelled` on purpose: cancelled means a real booking
    # fell through. Archived means it never should have counted toward
    # go-live at all -- pre-launch/iVvy-era clutter cleared in bulk ahead
    # of a real cutover date, not an outcome anyone decided against. Never
    # reachable via the normal staff status-change dropdown (see
    # LEGAL_TRANSITIONS) -- only ever set by the one-off
    # app.archive_bookings_before script.
    archived = "archived"


booking_status_enum = SAEnum(BookingStatus, name="booking_status", native_enum=True)


class MinReductionReasonCode(str, enum.Enum):
    friday_fill = "friday_fill"
    weekend_gap = "weekend_gap"
    returning_client = "returning_client"
    spend_clears_anyway = "spend_clears_anyway"
    aaron_discretion = "aaron_discretion"


min_reduction_reason_enum = SAEnum(MinReductionReasonCode, name="min_reduction_reason", native_enum=True)

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

    # NULL for a normal, single-space booking. Set when a single real event
    # needs two physical spaces at once -- see app.services.booking.
    # add_linked_space. The row this points at is the "parent": it alone
    # carries the contact, documents, invoices, and wizard session. This
    # row is purely a second space-and-time slot for the same event, and
    # sits behind the exact same exclusion constraint as any other booking
    # -- nothing about double-booking protection changes for a linked row.
    parent_booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True
    )

    # Nullable: a real enquiry can arrive with no date locked in yet
    # ("not sure of dates, are you flexible?") -- see
    # app.services.enquiry_classification's missing_event_date flag rather
    # than rejecting or guessing a date for it.
    event_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
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
            "CASE WHEN event_date IS NULL OR start_time IS NULL OR end_time IS NULL THEN NULL "
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
    # STAFF-FACING working notes. Displayed in the admin and carried into
    # the Event Order's INTERNAL block, never into anything a client reads.
    # It used to default into the client-facing "Special Notes" section of
    # a generated Event Order, which meant anything typed here published
    # itself the moment a BEO was made -- see document_generation.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The client's OWN words from the enquiry form, verbatim and separate
    # from anything staff wrote. Untrusted input by definition (Phase 2
    # brief section 7): it is the one field a client can write that reaches
    # the AI, so keeping it in its own column makes "treat this as
    # untrusted" a property of the data rather than a rule in a prompt.
    enquiry_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Captured at enquiry time so the ivvy.com.au marketplace's actual
    # contribution as a lead source is measurable before deciding whether
    # to cancel that listing -- see app/services/lead_analytics.py.
    lead_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lead_referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Fine-grained ad-attribution data -- UTM parameters and click IDs
    # (gclid, fbclid), captured client-side at first landing and persisted
    # through the whole browsing session so a return visit or a
    # later-in-session enquiry still carries them (see
    # app/templates/enquiry.html and app.services.attribution). Deliberately
    # separate from lead_source/lead_referrer above -- that is a coarser,
    # staff-facing bucket; this is the ad-platform-level record.
    #
    # NULL on both columns means attribution was never attempted -- a
    # staff-entered, iVvy-imported, or phone booking. That's a different,
    # honest fact from a real visitor who genuinely had nothing to
    # attribute (a real bundle exists, with referrer_category "unknown")
    # -- see app.services.attribution.build_touch's own comment.
    #
    # Pure data. Nothing in this codebase may ever branch pricing, routing,
    # classification, or policy on these values -- they arrive
    # unauthenticated from the client and are trivially forgeable.
    first_touch_attribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_touch_attribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

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

    # The date pizza pricing is quoted against (see
    # app.services.catalogue.resolve_pizza_price) -- NOT NULL, defaults to
    # created_at's date at booking creation. Deliberately a separate field
    # rather than reading created_at directly: for a booking imported from a
    # prior system, created_at is the import timestamp, not when the client
    # was actually quoted, and pizza pricing must never silently move to a
    # later, higher rate just because a real booking was typed into this
    # system after the fact.
    pricing_locked_at: Mapped[dt.date] = mapped_column(Date, nullable=False)

    # The Master Policy doc is explicit: "never read the minimum from the
    # space when producing a contract, invoice, Event Order or shortfall
    # calculation. Only ever from the booking." NOT NULL (not a nullable
    # fallback-to-space-default) is deliberate -- it means no future
    # consumer can accidentally read Space.standard_min_adults instead,
    # which is exactly the bug class the policy doc is warning about.
    # Defaults to the space's standard minimum at booking creation; only
    # changes on Aaron's explicit approval (see agreed_min_reduction_reason).
    agreed_min_adults: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL unless the minimum has actually been reduced from the space
    # standard -- set together with agreed_min_adults, never independently.
    agreed_min_reduction_reason: Mapped[MinReductionReasonCode | None] = mapped_column(
        min_reduction_reason_enum, nullable=True
    )

    # Staff-write-only (no route in this codebase lets a client set this).
    # Master Policy v1.3: new bookings get no outside food/cakes by default;
    # existing/grandfathered bookings are flagged true individually.
    outside_cake_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # When food service actually starts -- distinct from start_time (the
    # hire period's arrival time), which this may lag.
    food_service_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    # When guests actually arrive -- frequently later than start_time (the
    # host's own access). Client-known, captured by the wizard's basics
    # step, staff-editable on the BEO edit screen.
    guest_arrival_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    # Run-sheet moments (speeches, cake cutting, raffle draws):
    # [{"time": "HH:MM"|null, "label": str}]. JSONB, not a table -- purely
    # display lines on the Event Order timeline, nothing acts on a moment
    # individually the way staff act on a vendor's bump-in.
    key_moments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Staff-only (never asked in the wizard): pack-down / collection
    # arrangements, e.g. "decorator collects arch Sunday 10am".
    pack_down_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Standard is 2:00pm (see app.services.validation.SETUP_ACCESS_STANDARD_TIME).
    # setup_access_confirmed is deliberately tri-state: NULL = never
    # requested, False = requested earlier than standard and pending
    # Aaron's confirmation (never promised automatically), True = confirmed
    # (standard-or-later requests are auto-True; an early request only
    # becomes True via an explicit staff action).
    setup_access_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    setup_access_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # A hold is a Booking at 'tentative' status -- this is the only field
    # that distinguishes "a soft hold with a known expiry" from an ordinary
    # tentative booking mid-pipeline. Only meaningful while status is
    # tentative; NULL means either "not a hold" or "deliberately
    # open-ended", both of which render the same way (see
    # app.services.calendar).
    hold_expires_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # Set the moment a human changes status by hand (the staff dropdown, via
    # transition_status). While set, the automatic transitions
    # (auto_hold_on_send, auto_confirm_if_ready) leave this booking alone:
    # "manual override always wins" -- a deliberately confirmed, deposit-
    # waived booking must never be walked anywhere by automation. Cleared by
    # clear_status_pin ("hand back to automation"). NULL = automation may act.
    status_pinned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def status_pinned(self) -> bool:
        return self.status_pinned_at is not None

    # Set once, the first time the venue's own new-enquiry notification
    # email (app.services.enquiry_classification.notify_new_enquiry)
    # actually sends successfully. NULL means either "never attempted"
    # (an iVvy import, a manually-placed hold -- notify_new_enquiry is
    # never called for those) or "attempted and still failing" -- the two
    # are told apart by whether an "enquiry_notification_failed"
    # BookingEvent exists, not by this column alone. See
    # get_enquiry_notification_failures.
    enquiry_notification_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When the BROWSER dispatched each ad conversion for this enquiry --
    # recorded by a beacon AFTER gtag/fbq actually run, never at server
    # render. NULL = not yet dispatched, so the thank-you page keeps
    # offering that platform's snippet until the browser confirms it fired:
    # a closed tab or a blocked tag can't permanently suppress a real
    # conversion, while a refresh/Back/Forward after a confirmed dispatch
    # won't re-fire it. Per platform, so one being blocked never suppresses
    # the other. Only ever set for genuine public web enquiries (they carry
    # first_touch_attribution); staff/imported bookings never fire one.
    # "dispatched" not "received": the browser attempted the send -- platform
    # receipt is verified separately (GA4 DebugView / Meta Test Events).
    ga4_conversion_dispatched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta_conversion_dispatched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    space: Mapped["Space"] = relationship(back_populates="bookings")
    contact: Mapped["Contact"] = relationship(back_populates="bookings")
    events: Mapped[list["BookingEvent"]] = relationship(back_populates="booking", order_by="BookingEvent.created_at")
    documents: Mapped[list["Document"]] = relationship(back_populates="booking", order_by="Document.version")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="booking", order_by="Invoice.created_at")
    wizard_session: Mapped["WizardSession | None"] = relationship(back_populates="booking", uselist=False)
    parent_booking: Mapped["Booking | None"] = relationship(
        remote_side=[id], back_populates="linked_bookings"
    )
    linked_bookings: Mapped[list["Booking"]] = relationship(back_populates="parent_booking")
    vendors: Mapped[list["BookingVendor"]] = relationship(
        back_populates="booking", order_by="BookingVendor.created_at"
    )

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
