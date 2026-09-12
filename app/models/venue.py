import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # `name` is the internal label ("Hamilton"); `trading_name` is what a
    # client sees ("Meantime Hamilton"). They are different on purpose and
    # have been since the first import -- never print `name`.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # --- identity -----------------------------------------------------------
    # Every one of these was a module constant in app/services/policy.py,
    # bound once at import as a Jinja global and stamped on every
    # client-facing document. The Entrance is a different COMPANY with its
    # own ABN, bank account and Stripe account, so there had to be somewhere
    # to put a second set (migration e1b6a44c7f83).
    #
    # All nullable. A venue with no address is one nobody has finished
    # setting up, and the honest answer to a missing value is to refuse to
    # print rather than invent one -- NOT NULL would instead force a
    # placeholder into the table, which is how "TBC" ends up on a contract.
    trading_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    abn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    bank_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bank_bsb: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bank_account_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    licence_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    licensed_manager: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Five characters, because reference_code is String(20) and the date and
    # suffix take the rest. Unique: two venues sharing a prefix would make
    # references ambiguous, and a reference never changes once a client
    # holds one.
    reference_prefix: Mapped[str | None] = mapped_column(String(5), nullable=True, unique=True)

    # The NAME of the environment variable holding this venue's Stripe
    # secret key -- never the key itself -- and the account id that key must
    # belong to. The second one is what lets a resolved credential be
    # CHECKED against the venue it was resolved for rather than trusted: a
    # mis-keyed payment link is minted inside the wrong company's Stripe
    # account, and that account signs its own completion event, so nothing
    # downstream catches it (review, 2026-09-11).
    stripe_secret_key_env: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    spaces: Mapped[list["Space"]] = relationship(back_populates="venue")
