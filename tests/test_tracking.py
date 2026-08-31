"""Tracking & attribution implementation (Aug 2026):

- gbraid/wbraid capture (first + last touch)
- concurrent-POST idempotency (advisory lock) -- one submission = one enquiry
- GA4 function_enquiry_submitted + Meta Lead fired once, on backend-confirmed
  public enquiries only, refresh/duplicate-safe, non-PII
- analytics never blocks the enquiry form
"""

import datetime as dt
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Contact
from app.services import attribution
from app.templating import templates

GA4 = "G-XM8C86CGM6"
PIXEL = "7461755457239404"


def _payload(**overrides):
    p = dict(
        first_name="Pat", last_name="Wilson", email="pat.tracking@example.com", phone="0400111222",
        event_name="Wilson Enquiry", event_date="2027-11-14", dates_flexible="false",
        event_type="Wedding", attendee_count=80, proposed_time_slot="Evening",
        comments="Keen to see the space.",
    )
    p.update(overrides)
    return p


@pytest.fixture()
def client(db, hamilton, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def tags_on(monkeypatch):
    """Enable the GA4/Pixel tags for a test (they default OFF so dev/test
    never load a real tag)."""
    monkeypatch.setitem(templates.env.globals, "ga4_measurement_id", GA4)
    monkeypatch.setitem(templates.env.globals, "meta_pixel_id", PIXEL)


# --- attribution: gbraid / wbraid ---------------------------------------------


def test_build_touch_captures_gbraid_and_wbraid():
    bundle = attribution.build_touch({"gbraid": "gb-123", "wbraid": "wb-456", "gclid": "gc-1"})
    assert bundle["gbraid"] == "gb-123"
    assert bundle["wbraid"] == "wb-456"
    assert bundle["gclid"] == "gc-1"


def test_gbraid_wbraid_classify_as_google_paid():
    assert attribution.summarize_channel({"gbraid": "x"}) == "Google Ads (paid)"
    assert attribution.summarize_channel({"wbraid": "x"}) == "Google Ads (paid)"


def test_enquiry_persists_gbraid_wbraid_first_and_last(client, db):
    attribution_json = json.dumps({
        "first_touch": {"gbraid": "gb-first", "wbraid": "wb-first", "utm_source": "google"},
        "last_touch": {"gbraid": "gb-last", "wbraid": "wb-last"},
    })
    resp = client.post("/enquiries", data={**_payload(), "attribution": attribution_json})
    assert resp.status_code == 303
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()
    assert booking.first_touch_attribution["gbraid"] == "gb-first"
    assert booking.first_touch_attribution["wbraid"] == "wb-first"
    assert booking.last_touch_attribution["gbraid"] == "gb-last"
    assert booking.last_touch_attribution["wbraid"] == "wb-last"


# --- concurrent-POST idempotency ----------------------------------------------


def test_concurrent_identical_enquiries_create_one_booking():
    """Two genuinely simultaneous identical submissions must create ONE
    enquiry, one contact, one reference_code -- the transaction-level
    advisory lock closes the race the 15s window alone can't. Real sessions
    + commits (not the rollback fixture) so the lock and duplicate check see
    committed state; a unique event name keeps the assertion independent of
    any other data."""
    from sqlalchemy import text
    from app.models import BookingEvent
    from app.seed import seed as seed_hamilton
    from app.services.enquiry_classification import create_enquiry_booking
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    venue_id = venue.id
    email = f"race.{uuid.uuid4().hex[:8]}@example.com"
    event_name = f"Race Event {uuid.uuid4().hex[:8]}"
    setup.close()

    barrier = threading.Barrier(2)
    results = {}

    def attempt(name):
        session = TestSessionLocal()
        try:
            v = session.get(type(venue), venue_id)
            barrier.wait(timeout=5)
            _booking, _dups, is_new = create_enquiry_booking(
                session, venue=v, full_name="Race Tester", email=email, phone=None,
                event_name=event_name, event_type="birthday", event_date=dt.date(2027, 6, 6),
                proposed_time_slot=None, attendee_count=40, adult_count=None, company_name=None,
                dates_flexible=False, comments=None, lead_source="website", lead_referrer=None,
                actor="test", first_touch_attribution={"referrer": None}, last_touch_attribution={"referrer": None},
            )
            results[name] = is_new
        except Exception as exc:  # noqa: BLE001 -- surface any thread error to the assertion
            results[name] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = TestSessionLocal()
    try:
        assert all(not isinstance(v, Exception) for v in results.values()), results
        # Exactly one created (is_new True), the other reused it (is_new False).
        assert sorted(results.values()) == [False, True], results
        assert check.query(Booking).filter_by(event_name=event_name).count() == 1, "one enquiry, not two"
        assert check.query(Contact).filter_by(email=email).count() == 1, "one contact, not a duplicate"
    finally:
        # Purge under the append-only-events escape hatch.
        check.execute(text("SET LOCAL app.allow_booking_purge='on'"))
        for b in check.query(Booking).filter_by(event_name=event_name).all():
            check.execute(text("DELETE FROM booking_events WHERE booking_id=:i"), {"i": b.id})
        check.query(Booking).filter_by(event_name=event_name).delete(synchronize_session=False)
        check.query(Contact).filter_by(email=email).delete(synchronize_session=False)
        check.commit()
        check.close()


# --- conversion firing on the thank-you page ----------------------------------


def _submit_and_thanks_url(client) -> str:
    resp = client.post("/enquiries", data=_payload())
    assert resp.status_code == 303
    return resp.headers["location"]


def _booking_id_from_thanks(url: str) -> str:
    # /enquiries/<uuid>/thanks
    return url.split("/enquiries/")[1].split("/thanks")[0]


def test_thanks_renders_snippet_but_writes_nothing_at_render(client, db, tags_on):
    """Rendering the thank-you page is NOT "emitted". The snippet is offered,
    but both dispatch flags stay NULL until the browser confirms it fired --
    so a closed tab / blocked tag before dispatch cannot suppress a real
    conversion."""
    thanks = _submit_and_thanks_url(client)
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()
    assert booking.ga4_conversion_dispatched_at is None
    assert booking.meta_conversion_dispatched_at is None

    first = client.get(thanks)
    assert first.status_code == 200
    assert "function_enquiry_submitted" in first.text
    assert "'Lead'" in first.text
    assert booking.reference_code in first.text

    # No beacon yet -> flags remain NULL. The page rendering did not mark it.
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is None
    assert booking.meta_conversion_dispatched_at is None

    # And because nothing was recorded, a reload STILL offers the snippet:
    # a conversion is never permanently suppressed before a browser dispatch.
    second = client.get(thanks)
    assert "function_enquiry_submitted" in second.text
    assert "'Lead'" in second.text


def test_browser_dispatch_beacon_records_then_refresh_does_not_refire(client, db, tags_on):
    """The browser confirms each platform dispatched via a beacon; only then
    is that platform recorded, and only then does a refresh/Back-Forward stop
    rendering it. One enquiry = one GA4 event + one Meta Lead."""
    thanks = _submit_and_thanks_url(client)
    booking_id = _booking_id_from_thanks(thanks)
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()

    assert client.post(f"/enquiries/{booking_id}/conversion/ga4").status_code == 204
    assert client.post(f"/enquiries/{booking_id}/conversion/meta").status_code == 204

    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is not None
    assert booking.meta_conversion_dispatched_at is not None

    # Refresh: both dispatched -> snippet no longer rendered for either.
    refreshed = client.get(thanks)
    assert refreshed.status_code == 200
    assert "function_enquiry_submitted" not in refreshed.text
    assert "'Lead'" not in refreshed.text


def test_partial_dispatch_leaves_the_other_platform_free_to_fire(client, db, tags_on):
    """GA4 blocked while Meta fires (or vice versa): only the platform the
    browser actually dispatched is recorded, so the blocked one is still
    offered on the next load rather than being suppressed with it."""
    thanks = _submit_and_thanks_url(client)
    booking_id = _booking_id_from_thanks(thanks)
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()

    # Only Meta confirmed dispatch this time.
    assert client.post(f"/enquiries/{booking_id}/conversion/meta").status_code == 204
    db.refresh(booking)
    assert booking.meta_conversion_dispatched_at is not None
    assert booking.ga4_conversion_dispatched_at is None

    # Next load: Meta suppressed, GA4 still offered.
    html = client.get(thanks).text
    assert "function_enquiry_submitted" in html   # GA4 still eligible
    assert "'Lead'" not in html                    # Meta already dispatched

    # GA4 then confirms; now neither is offered.
    assert client.post(f"/enquiries/{booking_id}/conversion/ga4").status_code == 204
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is not None
    html2 = client.get(thanks).text
    assert "function_enquiry_submitted" not in html2
    assert "'Lead'" not in html2


def test_dispatch_beacon_is_idempotent(client, db, tags_on):
    """A replayed beacon (double dispatch, retried sendBeacon) must not move
    the recorded timestamp -- the record is the FIRST browser dispatch."""
    thanks = _submit_and_thanks_url(client)
    booking_id = _booking_id_from_thanks(thanks)
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()

    assert client.post(f"/enquiries/{booking_id}/conversion/ga4").status_code == 204
    db.refresh(booking)
    first_ts = booking.ga4_conversion_dispatched_at
    assert first_ts is not None

    assert client.post(f"/enquiries/{booking_id}/conversion/ga4").status_code == 204
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at == first_ts  # unchanged


def test_dispatch_beacon_unknown_platform_is_noop(client, db, tags_on):
    thanks = _submit_and_thanks_url(client)
    booking_id = _booking_id_from_thanks(thanks)
    booking = db.query(Booking).filter_by(event_name="Wilson Enquiry").one()

    assert client.post(f"/enquiries/{booking_id}/conversion/tiktok").status_code == 204
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is None
    assert booking.meta_conversion_dispatched_at is None


def test_conversion_snippet_carries_no_pii(client, db, tags_on):
    thanks = _submit_and_thanks_url(client)
    html = client.get(thanks).text
    # The lead id is fine; personal data must never appear in the snippet.
    assert "pat.tracking@example.com" not in html
    assert "0400111222" not in html
    # The firing snippet references only lead_id / venue / source_system / enquiry_type.
    assert "source_system" in html and "hamilton" in html


def test_staff_booking_thanks_does_not_emit_and_rejects_beacon(client, db, loft):
    """A booking with no first_touch_attribution (staff-entered / imported)
    is not a public web enquiry and must never fire an ad conversion, even if
    its thanks URL is opened or a forged beacon is sent to it."""
    from app.services.booking import create_booking

    contact = Contact(name="Phone Caller", email="phone@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 7, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Phoned In",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="staff",
    )  # no attribution passed -> first_touch_attribution is NULL
    templates.env.globals["ga4_measurement_id"] = GA4
    try:
        resp = client.get(f"/enquiries/{booking.id}/thanks")
        assert resp.status_code == 200
        assert "function_enquiry_submitted" not in resp.text

        # A forged beacon must not record a dispatch on a non-public booking.
        assert client.post(f"/enquiries/{booking.id}/conversion/ga4").status_code == 204
        db.refresh(booking)
        assert booking.ga4_conversion_dispatched_at is None
        assert booking.meta_conversion_dispatched_at is None
    finally:
        templates.env.globals["ga4_measurement_id"] = ""


# --- base tags gated by configuration -----------------------------------------


def test_tags_render_only_when_configured(client):
    # Default: both empty -> no GA4, no Pixel on the enquiry page.
    off = client.get("/enquire").text
    assert "googletagmanager.com/gtag/js" not in off
    assert "fbevents.js" not in off


def test_tags_render_when_configured(client, tags_on):
    on = client.get("/enquire").text
    assert f"gtag/js?id={GA4}" in on
    assert f"fbq('init', '{PIXEL}')" in on
    assert "fbq('track', 'PageView')" in on


# --- failure & isolation ------------------------------------------------------


def test_validation_failure_creates_no_booking_no_conversion(client, db):
    resp = client.post("/enquiries", data={**_payload(), "email": "not-an-email"})
    assert resp.status_code == 422
    assert db.query(Booking).filter_by(event_name="Wilson Enquiry").count() == 0


def test_tracking_does_not_alter_the_enquiry_form(client, tags_on):
    """Analytics is additive: the form's action, hidden attribution field and
    submit must be intact whether or not tags are present."""
    html = client.get("/enquire").text
    assert 'action="/enquiries"' in html
    assert 'name="attribution"' in html
    assert "Submit Inquiry" in html
