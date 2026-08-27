import datetime as dt
import enum
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils import generate_access_token as _generate_access_token


class DocumentType(str, enum.Enum):
    agreement = "agreement"
    beo = "beo"


class DocumentStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    viewed = "viewed"
    signed = "signed"


document_type_enum = SAEnum(DocumentType, name="document_type", native_enum=True)
document_status_enum = SAEnum(DocumentStatus, name="document_status", native_enum=True)


def generate_access_token() -> str:
    # Re-exported rather than inlined: this name is the column default and
    # is referenced in migrations, so it stays put while the actual
    # generation lives in one shared place (see app.utils).
    return _generate_access_token()


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    type: Mapped[DocumentType] = mapped_column(document_type_enum, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(document_status_enum, nullable=False, default=DocumentStatus.draft)

    # True only for the newest version of (booking_id, type) -- enforced by
    # the partial unique index below, so "get the current BEO" is a plain
    # indexed lookup instead of a MAX(version) scan. Superseded versions
    # keep is_current=False forever: they are never deleted or edited, so a
    # link that was already sent keeps resolving to exactly what was sent.
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    access_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, default=generate_access_token)

    # Set once, the first time the public link is opened -- distinct from
    # `status == viewed` (which a client's subsequent visits don't change)
    # so staff can see *when* a sent link was actually opened, not just
    # whether it eventually was.
    viewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signer_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # long enough for IPv6

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(back_populates="documents")

    __table_args__ = (
        UniqueConstraint("booking_id", "type", "version", name="uq_document_booking_type_version"),
        Index(
            "uq_document_current_per_booking_type",
            "booking_id",
            "type",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )
