import datetime as dt
import uuid

from sqlalchemy import Boolean, Date, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PublicHoliday(Base):
    """Policy data, not code: the public-holiday surcharge needs to know
    which dates are public holidays, and that list changes every year. A
    table that someone can correct or extend beats a hardcoded list that
    quietly goes stale.
    """

    __tablename__ = "public_holidays"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    holiday_date: Mapped[dt.date] = mapped_column(Date, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(10), nullable=False, default="NSW")

    # The NSW "Bank Holiday" (first Monday in August) is a declared public
    # holiday, but it's specific to banks/financial institutions -- most
    # hospitality awards don't treat it as a trading public holiday, so it
    # shouldn't trigger the event surcharge the way Christmas Day or ANZAC
    # Day would. Worth Aaron confirming against the actual award/EBA.
    applies_to_surcharge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
