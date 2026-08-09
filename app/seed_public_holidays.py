"""Idempotent seed of NSW public holidays for 2026-2027, verified against
https://www.nsw.gov.au/about-nsw/public-holidays directly (not a
third-party aggregator) during this build. Extend this list for future
years rather than guessing dates from memory -- getting one wrong means
over- or under-charging a real invoice.

Run with: python -m app.seed_public_holidays
"""

import datetime as dt

from app.database import SessionLocal
from app.models import PublicHoliday

# (date, name, applies_to_surcharge)
# Bank Holiday is finance-sector only, not a general hospitality trading
# holiday -- see app/models/public_holiday.py for the reasoning. Worth
# Aaron confirming against the actual award/EBA.
NSW_PUBLIC_HOLIDAYS = [
    (dt.date(2026, 1, 1), "New Year's Day", True),
    (dt.date(2026, 1, 26), "Australia Day", True),
    (dt.date(2026, 4, 3), "Good Friday", True),
    (dt.date(2026, 4, 4), "Easter Saturday", True),
    (dt.date(2026, 4, 5), "Easter Sunday", True),
    (dt.date(2026, 4, 6), "Easter Monday", True),
    (dt.date(2026, 4, 25), "Anzac Day", True),
    (dt.date(2026, 4, 27), "Anzac Day (additional day)", True),
    (dt.date(2026, 6, 8), "King's Birthday", True),
    (dt.date(2026, 8, 3), "Bank Holiday", False),
    (dt.date(2026, 10, 5), "Labour Day", True),
    (dt.date(2026, 12, 25), "Christmas Day", True),
    (dt.date(2026, 12, 26), "Boxing Day", True),
    (dt.date(2026, 12, 28), "Boxing Day (additional day)", True),
    (dt.date(2027, 1, 1), "New Year's Day", True),
    (dt.date(2027, 1, 26), "Australia Day", True),
    (dt.date(2027, 3, 26), "Good Friday", True),
    (dt.date(2027, 3, 27), "Easter Saturday", True),
    (dt.date(2027, 3, 28), "Easter Sunday", True),
    (dt.date(2027, 3, 29), "Easter Monday", True),
    (dt.date(2027, 4, 25), "Anzac Day", True),
    (dt.date(2027, 4, 26), "Anzac Day (additional day)", True),
    (dt.date(2027, 6, 14), "King's Birthday", True),
    (dt.date(2027, 8, 2), "Bank Holiday", False),
    (dt.date(2027, 10, 4), "Labour Day", True),
    (dt.date(2027, 12, 25), "Christmas Day", True),
    (dt.date(2027, 12, 26), "Boxing Day", True),
    (dt.date(2027, 12, 27), "Christmas Day (additional day)", True),
    (dt.date(2027, 12, 28), "Boxing Day (additional day)", True),
]


def seed(db=None) -> int:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        existing_dates = {h.holiday_date for h in db.query(PublicHoliday)}
        added = 0
        for holiday_date, name, applies_to_surcharge in NSW_PUBLIC_HOLIDAYS:
            if holiday_date in existing_dates:
                continue
            db.add(
                PublicHoliday(
                    holiday_date=holiday_date,
                    name=name,
                    region="NSW",
                    applies_to_surcharge=applies_to_surcharge,
                )
            )
            added += 1
        db.commit()
        return added
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":
    added = seed()
    print(f"Seeded {added} NSW public holidays.")
