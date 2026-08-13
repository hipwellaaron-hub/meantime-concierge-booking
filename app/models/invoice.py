import datetime as dt
import enum
import secrets
import uuid
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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
    return secrets.token_urlsafe(32)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    # A short, human-readable reference for a client to quote back ("my
    # invoice number is 1004") -- the UUID id/access_token are never fit
    # for that. Assigned by a Postgres sequence (invoice_number_seq, see
    # the migration) starting fresh at 1001: this is Concierge's own
    # numbering, not a continuation of the prior iVvy sequence, since
    # there's no reliable source for exactly where that one left off.
    invoice_number: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True, server_default=text("nextval('invoice_number_seq')")
    )
    type: Mapped[InvoiceType] = mapped_column(invoice_type_enum, nullable=False)
    line_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Public-holiday surcharge only. A card surcharge, where still legal,
    # is calculated per-payment (see app.services.policy) rather than
    # baked into this stored total -- see the Phase 3 build notes for why.
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

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", order_by="Payment.received_at")
