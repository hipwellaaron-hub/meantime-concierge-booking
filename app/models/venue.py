import uuid

from sqlalchemy import SmallInteger, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
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

    # The days this venue TRADES, as Python weekday numbers (Monday=0 ...
    # Sunday=6) -- the same numbering `date.weekday()` returns, so a check
    # is `d.weekday() in venue.trading_days` with no conversion.
    #
    # Open days rather than closed ones, so NULL unambiguously means "nobody
    # has said" and [] means "open no days", which is a different and equally
    # sayable thing. NULL is never treated as "same as Hamilton": that is the
    # guess that puts a function on a day the kitchen is shut.
    #
    # Hamilton and The Entrance are both Wed-Sun (Aaron, 2026-09-12).
    trading_days: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger), nullable=True)

    # Who receives THIS venue's share of the staff digest. NULL means "the
    # process-wide DIGEST_RECIPIENT_EMAIL", which is today's behaviour; there
    # is no address to put here that is not already in that variable, and
    # copying it in would give one fact two sources.
    #
    # The sender groups venues BY recipient, so two venues pointing at the
    # same address stay ONE email with a section each (Aaron reads it on a
    # phone first thing; two emails means one gets skimmed), and pointing one
    # venue elsewhere splits them with no code change.
    digest_recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

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
    #
    # FILLING stripe_account_id CLOSES THE ROLLBACK WINDOW. While it is NULL,
    # create_payment_link returns no account and record_payment_link stores a
    # bare link id, which the previous build can still read. From the first
    # link minted after it is set, entries are {"id":…, "account":…} dicts --
    # and the previous build's deactivation loop hands a dict straight to
    # stripe.PaymentLink.modify, which raises TypeError (not a StripeError,
    # so it is not caught) and abandons the rest of that invoice's links
    # undeactivated. Fill it once this build is settled, not during the
    # window where a rollback is still on the table (review, 2026-09-12).
    stripe_secret_key_env: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # And the variable holding this venue's WEBHOOK signing secret. Separate
    # from the API key because Stripe issues one per endpoint, and each venue
    # has its own endpoint -- construct_event verifies against exactly one
    # secret, so one endpoint cannot serve two accounts.
    stripe_webhook_secret_env: Mapped[str | None] = mapped_column(String(64), nullable=True)

    spaces: Mapped[list["Space"]] = relationship(back_populates="venue")
