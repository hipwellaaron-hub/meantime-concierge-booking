import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VenueInvoiceCounter(Base):
    """One venue's tax-invoice register: the number its NEXT invoice takes.

    Two legal entities share this database -- Meantime Pty Ltd and Nice Try
    Events Pty Ltd -- and each keeps its own register, because a company's
    invoice numbers are its own and an accountant handed a series with holes
    in it cannot tell a missing invoice from another company's.

    NOT WRITTEN BY THE APPLICATION. A BEFORE INSERT trigger on `invoices`
    takes the next number with a single

        UPDATE ... SET next_number = next_number + 1 RETURNING next_number - 1

    which is a row lock: two invoices raised at the same instant for one
    venue take consecutive numbers, and a rolled-back transaction gives its
    number back rather than burning it -- something a Postgres sequence
    cannot do, and the reason this is a table rather than a sequence per
    venue. See migration f3d9b7c1a468.

    Declared here so alembic's autogenerate knows the table exists. Nothing
    in the application reads it; it is the allocator's own state, and the
    number it produced is on the invoice.
    """

    __tablename__ = "venue_invoice_counters"
    __table_args__ = (
        CheckConstraint("next_number > 0", name="ck_venue_invoice_counters_positive"),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venues.id"), primary_key=True
    )
    next_number: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
