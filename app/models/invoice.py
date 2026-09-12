import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, UniqueConstraint, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils import generate_access_token as _generate_access_token


class InvoiceType(str, enum.Enum):
    deposit = "deposit"
    final = "final"


class InvoiceStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    paid = "paid"
    cancelled = "cancelled"


invoice_type_enum = SAEnum(InvoiceType, name="invoice_type", native_enum=True)
invoice_status_enum = SAEnum(InvoiceStatus, name="invoice_status", native_enum=True)


def generate_access_token() -> str:
    # See app.models.document.generate_access_token on why this indirection.
    return _generate_access_token()


class Invoice(Base):
    __tablename__ = "invoices"
    # Declared so autogenerate does not propose dropping it. The
    # integer is unique WITHIN a venue; the reference above is unique
    # across all of them.
    __table_args__ = (UniqueConstraint("venue_id", "invoice_number", name="uq_invoices_venue_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    # WHICH COMPANY'S INVOICE THIS IS. Two legal entities share this table,
    # and each keeps its own register.
    #
    # Never written by the application: a BEFORE INSERT trigger takes it
    # from the booking, whatever the caller supplied, and a second trigger
    # refuses to let it change afterwards (migration f3d9b7c1a468). So it
    # cannot disagree with the booking it belongs to -- the same guarantee
    # fk_bookings_space_venue gives bookings, by a different mechanism.
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venues.id"), nullable=False
    )

    # This venue's own running number. Unique PER VENUE, not globally:
    # HAM-1004 and ENT-1004 are two different companies' invoices and both
    # are correct. Starts at 1001 for each register.
    #
    # Assigned by the same trigger, from venue_invoice_counters, with one
    # UPDATE ... RETURNING -- a row lock, so two invoices raised at the
    # same instant for one venue take consecutive numbers, and a
    # rolled-back transaction gives its number back rather than burning it.
    invoice_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # WHAT THE CLIENT READS AND QUOTES BACK: "HAM-1004". The venue's
    # reference_prefix and its number, the same shape as the booking
    # reference printed beside it.
    #
    # STORED, not derived, for the reason bookings.reference_code is
    # stored: a client holds it, so editing a venue row must not change
    # what is on an invoice already sent. It carries the GLOBAL unique that
    # invoice_number used to carry, which is what makes a quoted reference
    # resolve to exactly one invoice across both companies.
    invoice_reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    type: Mapped[InvoiceType] = mapped_column(invoice_type_enum, nullable=False)
    line_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Public-holiday surcharge only, and only for events BEFORE
    # policy.SURCHARGE_END_DATE -- the charge ended on 2026-10-01 and is
    # sunset by event date so a past event's figures stay reproducible.
    # Stored, not recomputed at render, which is why a sent invoice keeps
    # what it was sent with.
    #
    # This used to add "a card surcharge, where still legal, is calculated
    # per-payment (see app.services.policy)". That pointer went nowhere the
    # moment the card surcharge was deleted on 2026-09-11: policy.py has no
    # card surcharge left at all, so the sentence described a charge that no
    # longer exists and sent the reader to a module that never mentions it.
    surcharge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(invoice_status_enum, nullable=False, default=InvoiceStatus.draft)
    due_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, the first time the public link is opened. Deliberately not
    # a new `status` value (e.g. "viewed") -- every place that currently
    # means "sent and not yet paid" (the unpaid-invoices dashboard count,
    # the overdue-invoice digest) reads status == sent, and a viewed
    # invoice is still exactly that; a separate status would silently
    # drop it from both.
    viewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    access_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, default=generate_access_token)

    # Every Stripe Payment Link ever created for this invoice. A fresh one
    # is generated on each invoice-page view (see stripe_integration's own
    # docstring), so there can be several live at once, and a Payment Link
    # has no expiry of its own -- unlike a Checkout Session, it stays
    # payable indefinitely until deactivated. Recorded here so
    # cancel_invoice has something to deactivate; see
    # stripe_integration.deactivate_payment_links.
    stripe_payment_link_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )

    # Legacy upload (iVvy migration): an inert record of a deposit already
    # invoiced/paid in a prior system. is_legacy marks it read-only -- never
    # edited, revised, re-sent, or taking a new payment. legacy_file is the
    # original PDF as opaque bytes; legacy_snapshot is the booking facts the
    # PDF represents, for mismatch detection.
    is_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    legacy_file: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    legacy_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    legacy_source_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    legacy_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", order_by="Payment.received_at")
