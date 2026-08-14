import datetime as dt
import re

from app.models.booking import BookingStatus
from app.services.attribution import build_touch
from app.services.booking import change_status, create_booking


def _booking(db, space, *, event_name, status=BookingStatus.enquiry, first_touch=None):
    booking = create_booking(
        db, space_id=space.id, contact_id=None, event_date=dt.date(2027, 3, 1),
        event_name=event_name, event_type=None, adult_count=20, child_count=0,
        notes=None, actor="test", first_touch_attribution=first_touch, last_touch_attribution=first_touch,
    )
    if status != BookingStatus.enquiry:
        change_status(db, booking, status, actor="test")
    return booking


def test_attribution_report_renders(admin_client):
    resp = admin_client.get("/admin/reports/attribution")
    assert resp.status_code == 200
    assert "Attribution report" in resp.text


def test_attribution_report_counts_confirmed_separately_from_all(admin_client, db, loft):
    google_touch = build_touch({"gclid": "abc123"})
    _booking(db, loft, event_name="Confirmed Google Ad Booking", status=BookingStatus.confirmed, first_touch=google_touch)
    _booking(db, loft, event_name="Still Just An Enquiry", status=BookingStatus.enquiry, first_touch=google_touch)

    resp = admin_client.get("/admin/reports/attribution")
    assert resp.status_code == 200
    counts = [int(n) for n in re.findall(r'<div class="num">(\d+)</div>', resp.text)]
    assert counts == [2, 1]  # all_total, confirmed_total
    confirmed_section = resp.text.split("Confirmed bookings by channel")[1].split("All enquiries by channel")[0]
    assert "Google Ads (paid)" in confirmed_section


def test_attribution_report_respects_date_range(admin_client, db, loft):
    booking = _booking(db, loft, event_name="Old Booking", status=BookingStatus.confirmed, first_touch=build_touch({"gclid": "abc"}))
    booking.created_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    db.commit()

    resp = admin_client.get("/admin/reports/attribution", params={"since": "2026-01-01", "until": "2026-12-31"})
    assert resp.status_code == 200
    assert "No confirmed bookings in this range" in resp.text or "No enquiries in this range" in resp.text
