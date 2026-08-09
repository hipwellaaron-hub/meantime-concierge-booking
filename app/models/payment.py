import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PaymentMethod(str, enum.Enum):
    bank_transfer = "bank_transfer"
    card = "card"
    cash = "cash"
    other = "other"


payment_method_enum = SAEnum(PaymentMethod, name="payment_method", native_enum=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(payment_method_enum, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Not in the original spec's field list, added because one invoice can
    # be split across multiple payers (e.g. three separate organisations
    # covering one final invoice) and there needs to be some way to tell
    # their payments apart without inventing a whole separate payers table.
    payer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    invoice: Mapped["Invoice"] = relationship(back_populates="payments")
