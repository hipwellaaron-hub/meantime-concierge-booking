"""A Meta conversion is attributed to the page it came from, and the setting
that names that page is a real setting.

conversions.meta_payload read `getattr(settings, "public_base_url", "")`.
Settings never defined that field and is declared extra="ignore", so a
PUBLIC_BASE_URL set on Railway was silently dropped and the getattr took
its default every time: a setting that read as configurable and was not.
It is a declared field now, and the payload reads it directly.

EVERY PROBE SETS THE FIELD TO SOMETHING THE DASHBOARD IS NOT. With it
empty -- which is production today, and correct -- the fallback prints the
right URL and a payload that ignored the field would pass.
"""
import datetime as dt

from app.services import conversions
from app.services.booking import create_booking


def _booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def test_the_public_site_is_the_source_when_it_is_named(db, hamilton, loft, contact, monkeypatch):
    """THE one. PUBLIC_BASE_URL used to be ignored; now it is where the
    conversion says it came from."""
    monkeypatch.setattr(conversions.settings, "public_base_url", "https://www.meantime.com.au")
    monkeypatch.setattr(conversions.settings, "dashboard_base_url", "https://book.meantime.com.au")
    booking = _booking(db, loft, contact, "ZZMETA Public")

    url = conversions.meta_payload(booking)["data"][0]["event_source_url"]

    assert url.startswith("https://www.meantime.com.au/enquire"), url
    assert "book.meantime.com.au" not in url


def test_the_dashboard_is_the_source_when_nothing_is_named(db, hamilton, loft, contact, monkeypatch):
    """Today's configuration, and it must keep working."""
    monkeypatch.setattr(conversions.settings, "public_base_url", "")
    monkeypatch.setattr(conversions.settings, "dashboard_base_url", "https://book.meantime.com.au")
    booking = _booking(db, loft, contact, "ZZMETA Dashboard")

    url = conversions.meta_payload(booking)["data"][0]["event_source_url"]

    assert url.startswith("https://book.meantime.com.au/enquire"), url


def test_the_setting_is_declared_not_inferred():
    """The field exists on Settings, so a value in the environment is read
    rather than dropped by extra="ignore"."""
    from app.config import Settings

    assert "public_base_url" in Settings.model_fields


def test_the_venue_slug_still_lands_on_the_path(db, hamilton, loft, contact, monkeypatch):
    monkeypatch.setattr(conversions.settings, "public_base_url", "https://www.meantime.com.au")
    booking = _booking(db, loft, contact, "ZZMETA Slug")

    url = conversions.meta_payload(booking)["data"][0]["event_source_url"]

    assert url == f"https://www.meantime.com.au/enquire/{hamilton.slug}"
