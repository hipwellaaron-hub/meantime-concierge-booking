"""Tracking completion (2026-09-06 audit):

- parent-domain cookies: the gclid Google's linker cookie recorded on
  meantime.com.au, the fbclid in Meta's _fbc, and the website's own
  mt_touch_* cookies become touches; ordered by time, never merged
- submission identity: one submission_id = one lead, whatever the clock
- tracking context captured from cookies and headers, never contact data
- server-side Meta Conversions API copy with the pixel's event id;
  independent per-platform state; retry on failure only; no secret in a URL
- GA4 Measurement Protocol as a fallback that never runs beside a
  confirmed browser send
- everything off unless the production opt-in is set
"""

import base64
import datetime as dt
import json
import threading
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Booking, Contact
from app.models.conversion_dispatch import (
    CHANNEL_BROWSER, CHANNEL_SERVER, PLATFORM_GA4, PLATFORM_META, STATUS_ACCEPTED, STATUS_FAILED, STATUS_SENT, STATUS_SKIPPED,
    ConversionDispatch,
)
from app.services import attribution, conversions

GA4 = "G-XM8C86CGM6"
PIXEL = "7461755457239404"


def _payload(**overrides):
    p = dict(
        first_name="Pat", last_name="Wilson", email="pat.completion@example.com", phone="0400111222",
        event_name="Completion Enquiry", event_date="2027-11-14", dates_flexible="false",
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
def server_dispatch_on(monkeypatch):
    monkeypatch.setattr(settings, "tracking_server_dispatch_enabled", True)
    monkeypatch.setattr(settings, "ga4_measurement_id", GA4)
    monkeypatch.setattr(settings, "meta_pixel_id", PIXEL)
    monkeypatch.setattr(settings, "meta_capi_access_token", "EAAtest-token-never-logged")
    monkeypatch.setattr(settings, "ga4_api_secret", "ga4-secret-never-logged")
    monkeypatch.setattr(settings, "meta_capi_test_event_code", "")


class _Post:
    """Capture httpx.post; answer per URL substring."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for needle, answer in self.answers.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return httpx.Response(500, json={})


def _site_touch_cookie(**fields) -> str:
    raw = json.dumps(fields).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --- parent-domain cookies become touches --------------------------------------


def test_linker_cookie_gclid_becomes_the_first_touch_when_the_page_saw_only_a_referral():
    # Proven live 2026-09-06: a click landing on meantime.com.au leaves
    # _gcl_aw=GCL.<ts>.<gclid> readable on book.meantime.com.au, while the
    # enquiry page itself captured only "referrer: meantime.com.au".
    click_ts = 1788681860
    cookies = {"_gcl_aw": f"GCL.{click_ts}.TESTGCLID123", "_ga": "GA1.1.974737554.1785361318"}
    page_touch = attribution.build_touch({"referrer": "https://meantime.com.au/", "captured_at": "2026-09-06T08:04:55Z"})

    first, last = attribution.reconcile_touches(page_touch, page_touch, attribution.touches_from_cookies(cookies))

    assert first["gclid"] == "TESTGCLID123"
    assert first["captured_at"] == "2026-09-06T08:04:20+00:00"  # the click's own time, from the cookie
    assert last["gclid"] == "TESTGCLID123"
    assert first["utm_source"] is None  # nothing merged in from the referral touch


def test_touches_are_ordered_by_time_and_never_merged():
    # An old paid click (linker cookie) and a newer UTM visit (site cookie):
    # first is the click, last is the UTM visit, and neither carries the
    # other's fields.
    cookies = {
        "_gcl_aw": "GCL.1780000000.OLDCLICK",
        "mt_touch_last": _site_touch_cookie(utm_source="newsletter", utm_medium="email", captured_at="2026-09-01T00:00:00Z"),
    }
    # The enquiry page itself saw only the hop from meantime.com.au, which is
    # internal navigation, not a touch.
    page_touch = attribution.build_touch({"referrer": "https://meantime.com.au/functions.html", "captured_at": "2026-09-06T00:00:00Z"})
    first, last = attribution.reconcile_touches(page_touch, page_touch, attribution.touches_from_cookies(cookies))
    assert first["gclid"] == "OLDCLICK" and first["utm_source"] is None
    assert last["utm_source"] == "newsletter" and last["gclid"] is None


def test_a_campaign_seen_by_the_page_itself_still_wins_when_it_is_latest():
    cookies = {"_gcl_aw": "GCL.1780000000.OLDCLICK"}
    page_last = attribution.build_touch({"utm_source": "instagram", "utm_medium": "paid_social", "captured_at": "2026-09-06T10:00:00Z"})
    page_first = attribution.build_touch({"referrer": None, "captured_at": "2026-09-06T09:00:00Z"})
    first, last = attribution.reconcile_touches(page_first, page_last, attribution.touches_from_cookies(cookies))
    assert first["gclid"] == "OLDCLICK"
    assert last["utm_source"] == "instagram"


def test_the_same_click_in_url_and_cookie_is_one_touch():
    cookies = {"_gcl_aw": "GCL.1788681860.SAMECLICK"}
    page = attribution.build_touch({"gclid": "SAMECLICK", "utm_source": "google", "captured_at": "2026-09-05T13:00:00Z"})
    first, last = attribution.reconcile_touches(page, page, attribution.touches_from_cookies(cookies))
    assert first is page and last is page  # the cookie copy was not a second touch


def test_meta_click_cookie_and_malformed_cookies():
    cookies = {
        "_fbc": "fb.1.1788681860000.FBCLICKID",
        "_gcl_aw": "not-a-linker-cookie",
        "mt_touch_first": "!!!not base64!!!",
        "mt_touch_last": _site_touch_cookie(hello="world"),  # no signal -> ignored
    }
    touches = attribution.touches_from_cookies(cookies)
    assert len(touches) == 1
    assert touches[0]["fbclid"] == "FBCLICKID"


def test_no_signal_anywhere_keeps_the_pages_own_bundles():
    page = attribution.build_touch({"referrer": "https://meantime.com.au/"})
    first, last = attribution.reconcile_touches(page, page, attribution.touches_from_cookies({"_ga": "GA1.1.1.1"}))
    assert first is page and last is page


def test_submission_reads_the_parent_domain_cookies(client, db):
    resp = client.post(
        "/enquiries",
        data=_payload(),
        cookies={"_gcl_aw": "GCL.1788681860.COOKIEGCLID", "_ga": "GA1.1.974737554.1785361318",
                 "_ga_XM8C86CGM6": "GS2.1.s1788681860$o19$g1$t1788681896$j24$l0$h1379739795",
                 "_fbp": "fb.2.1785361318200.905305"},
        headers={"referer": "https://meantime.com.au/"},
    )
    assert resp.status_code == 303
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert booking.first_touch_attribution["gclid"] == "COOKIEGCLID"
    assert attribution.summarize_channel(booking.first_touch_attribution) == "Google Ads (paid)"
    context = booking.tracking_context
    assert context["ga_client_id"] == "974737554.1785361318"
    assert context["fbp"] == "fb.2.1785361318200.905305"
    assert "pat.completion@example.com" not in json.dumps(context)
    assert "Pat" not in json.dumps(context)


def test_session_id_is_read_from_the_stream_cookie(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ga4_measurement_id", GA4)
    client.post("/enquiries", data=_payload(),
                cookies={"_ga": "GA1.1.1.2", "_ga_XM8C86CGM6": "GS2.1.s1788681860$o19$g1$t1788681896$j24$l0$h1"})
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert booking.tracking_context["ga_session_id"] == "1788681860"


# --- submission identity ---------------------------------------------------------


def test_the_same_submission_id_resolves_to_the_same_lead_after_the_window(client, db, monkeypatch):
    from app.services import enquiry_classification
    monkeypatch.setattr(enquiry_classification, "DUPLICATE_SUBMISSION_WINDOW", dt.timedelta(seconds=0))
    sid = str(uuid.uuid4())
    first = client.post("/enquiries", data={**_payload(), "submission_id": sid})
    second = client.post("/enquiries", data={**_payload(), "submission_id": sid})
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert db.query(Booking).filter_by(event_name="Completion Enquiry").count() == 1
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert str(booking.submission_id) == sid


def test_a_fresh_submission_id_after_the_window_is_a_new_lead(client, db, monkeypatch):
    from app.services import enquiry_classification
    monkeypatch.setattr(enquiry_classification, "DUPLICATE_SUBMISSION_WINDOW", dt.timedelta(seconds=0))
    client.post("/enquiries", data={**_payload(), "submission_id": str(uuid.uuid4())})
    client.post("/enquiries", data={**_payload(), "submission_id": str(uuid.uuid4())})
    assert db.query(Booking).filter_by(event_name="Completion Enquiry").count() == 2


def test_a_garbage_submission_id_is_ignored_not_rejected(client, db):
    resp = client.post("/enquiries", data={**_payload(), "submission_id": "not-a-uuid"})
    assert resp.status_code == 303
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert booking.submission_id is None


def test_concurrent_repeats_with_one_submission_id_create_one_lead():
    from sqlalchemy import text
    from app.seed import seed as seed_hamilton
    from app.services.enquiry_classification import create_enquiry_booking
    from tests.conftest import TestSessionLocal

    setup = TestSessionLocal()
    venue = seed_hamilton(setup)
    venue_id = venue.id
    email = f"race.sid.{uuid.uuid4().hex[:8]}@example.com"
    event_name = f"Race SID {uuid.uuid4().hex[:8]}"
    sid = uuid.uuid4()
    setup.close()

    barrier = threading.Barrier(2)
    results = {}

    def attempt(name):
        session = TestSessionLocal()
        try:
            v = session.get(type(venue), venue_id)
            barrier.wait(timeout=5)
            _b, _d, is_new = create_enquiry_booking(
                session, venue=v, full_name="Race Tester", email=email, phone=None,
                event_name=event_name, event_type="corporate", event_date=dt.date(2027, 6, 6),
                proposed_time_slot=None, attendee_count=40, adult_count=None, company_name=None,
                dates_flexible=False, comments=None, lead_source="website", lead_referrer=None,
                actor="test", first_touch_attribution={"referrer": None}, last_touch_attribution={"referrer": None},
                submission_id=sid,
            )
            results[name] = is_new
        except Exception as exc:  # noqa: BLE001
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
        assert sorted(results.values()) == [False, True], results
        assert check.query(Booking).filter_by(submission_id=sid).count() == 1
    finally:
        check.execute(text("SET LOCAL app.allow_booking_purge='on'"))
        for b in check.query(Booking).filter_by(event_name=event_name).all():
            check.execute(text("DELETE FROM conversion_dispatches WHERE booking_id=:i"), {"i": b.id})
            check.execute(text("DELETE FROM booking_events WHERE booking_id=:i"), {"i": b.id})
        check.query(Booking).filter_by(event_name=event_name).delete(synchronize_session=False)
        check.query(Contact).filter_by(email=email).delete(synchronize_session=False)
        check.commit()
        check.close()


def test_the_form_renders_a_submission_id_field(client):
    html = client.get("/enquire").text
    assert 'name="submission_id"' in html
    assert "crypto.randomUUID" in html


# --- server-side Meta copy --------------------------------------------------------


def _public_booking(client, db) -> Booking:
    resp = client.post("/enquiries", data=_payload(), cookies={"_fbp": "fb.2.1.905305", "_fbc": "fb.1.1788681860000.FBC"},
                       headers={"user-agent": "Mozilla/5.0 test"})
    assert resp.status_code == 303
    return db.query(Booking).filter_by(event_name="Completion Enquiry").one()


def test_meta_server_copy_shares_the_pixels_event_id_and_carries_no_pii(client, db, server_dispatch_on, monkeypatch):
    booking = _public_booking(client, db)
    post = _Post({"graph.facebook.com": httpx.Response(200, json={"events_received": 1, "fbtrace_id": "AbC123"})})
    monkeypatch.setattr(httpx, "post", post)

    row = conversions.dispatch_meta(db, booking)

    assert row.status == STATUS_ACCEPTED and row.channel == CHANNEL_SERVER and row.platform == PLATFORM_META
    assert row.event_id == booking.reference_code
    assert row.receipt == {"events_received": 1, "fbtrace_id": "AbC123", "test_event": False}
    url, kwargs = post.calls[0]
    assert url == f"https://graph.facebook.com/{conversions.META_GRAPH_VERSION}/{PIXEL}/events"
    assert "access_token" not in url  # the token travels in the body, never a URL a log could catch
    body = kwargs["json"]
    event = body["data"][0]
    assert event["event_name"] == "Lead" and event["event_id"] == booking.reference_code
    assert event["action_source"] == "website"
    assert event["user_data"] == {"client_user_agent": "Mozilla/5.0 test", "fbp": "fb.2.1.905305", "fbc": "fb.1.1788681860000.FBC",
                                  "client_ip_address": "testclient"}
    serialised = json.dumps(body)
    for pii in ("pat.completion@example.com", "Pat", "Wilson", "0400111222", "Keen to see"):
        assert pii not in serialised
    assert "test_event_code" not in body


def test_meta_test_event_code_routes_to_test_events(client, db, server_dispatch_on, monkeypatch):
    monkeypatch.setattr(settings, "meta_capi_test_event_code", "TEST12345")
    booking = _public_booking(client, db)
    post = _Post({"graph.facebook.com": httpx.Response(200, json={"events_received": 1, "fbtrace_id": "t"})})
    monkeypatch.setattr(httpx, "post", post)
    row = conversions.dispatch_meta(db, booking)
    assert post.calls[0][1]["json"]["test_event_code"] == "TEST12345"
    assert row.receipt["test_event"] is True


def test_meta_failure_is_recorded_with_a_retry_time_and_no_secret(client, db, server_dispatch_on, monkeypatch):
    booking = _public_booking(client, db)
    post = _Post({"graph.facebook.com": httpx.Response(400, json={"error": {"message": "Invalid OAuth access token."}})})
    monkeypatch.setattr(httpx, "post", post)
    row = conversions.dispatch_meta(db, booking)
    assert row.status == STATUS_FAILED and row.attempts == 1
    assert row.next_attempt_at is not None
    assert "Invalid OAuth" in row.last_error
    assert "EAAtest" not in (row.last_error or "")


def test_transport_failure_then_retry_succeeds_and_does_not_resend_after(client, db, server_dispatch_on, monkeypatch):
    booking = _public_booking(client, db)
    post = _Post({"graph.facebook.com": httpx.ConnectError("boom")})
    monkeypatch.setattr(httpx, "post", post)
    now = dt.datetime.now(dt.timezone.utc)
    row = conversions.dispatch_meta(db, booking, now=now)
    assert row.status == STATUS_FAILED and "ConnectError" in row.last_error

    # The sweep before the backoff elapses does nothing; after it, retries once.
    post.answers = {"graph.facebook.com": httpx.Response(200, json={"events_received": 1, "fbtrace_id": "ok"})}
    conversions.run_sweep(db, now=now + dt.timedelta(minutes=1))
    assert len(post.calls) == 1
    summary = conversions.run_sweep(db, now=now + dt.timedelta(minutes=10))
    assert summary["meta_retried"] == 1 and summary["meta_accepted"] == 1
    db.refresh(row)
    assert row.status == STATUS_ACCEPTED and row.attempts == 2
    # Accepted stays accepted: another sweep sends nothing more.
    conversions.run_sweep(db, now=now + dt.timedelta(hours=5))
    assert len(post.calls) == 2


def test_one_platform_failing_does_not_touch_the_other(client, db, server_dispatch_on, monkeypatch):
    booking = _public_booking(client, db)
    # Browser confirmed GA4; Meta server send fails.
    assert client.post(f"/enquiries/{booking.id}/conversion/ga4").status_code == 204
    post = _Post({"graph.facebook.com": httpx.Response(500, json={})})
    monkeypatch.setattr(httpx, "post", post)
    conversions.run_sweep(db, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    rows = {(r.platform, r.channel): r for r in db.query(ConversionDispatch).filter_by(booking_id=booking.id)}
    assert rows[(PLATFORM_GA4, CHANNEL_BROWSER)].status == STATUS_SENT
    assert rows[(PLATFORM_META, CHANNEL_SERVER)].status == STATUS_FAILED
    assert (PLATFORM_GA4, CHANNEL_SERVER) not in rows  # browser confirmed: the fallback is never even considered
    assert all("google-analytics.com" not in url for url, _ in post.calls)  # GA4 was never re-sent


# --- GA4 fallback ------------------------------------------------------------------


def _ga4_booking(client, db) -> Booking:
    resp = client.post("/enquiries", data=_payload(), cookies={"_ga": "GA1.1.974737554.1785361318",
                                                                  "_ga_XM8C86CGM6": "GS2.1.s1788681860$o19$g1$t1$j1$l0$h1"})
    assert resp.status_code == 303
    return db.query(Booking).filter_by(event_name="Completion Enquiry").one()


def test_ga4_fallback_waits_for_the_grace_period_then_sends_once(client, db, server_dispatch_on, monkeypatch):
    booking = _ga4_booking(client, db)
    post = _Post({"google-analytics.com/mp/collect": httpx.Response(204), "graph.facebook.com": httpx.Response(200, json={"events_received": 1})})
    monkeypatch.setattr(httpx, "post", post)
    created = booking.created_at

    conversions.run_sweep(db, now=created + dt.timedelta(minutes=5))
    assert not [u for u, _ in post.calls if "google-analytics" in u]

    summary = conversions.run_sweep(db, now=created + dt.timedelta(minutes=45))
    assert summary["ga4_fallback_sent"] == 1
    url, kwargs = [c for c in post.calls if "google-analytics" in c[0]][0]
    assert url == conversions.GA4_MP_URL
    assert kwargs["params"] == {"measurement_id": GA4, "api_secret": "ga4-secret-never-logged"}
    body = kwargs["json"]
    assert body["client_id"] == "974737554.1785361318"
    event = body["events"][0]
    assert event["name"] == "function_enquiry_submitted"
    assert event["params"]["lead_id"] == booking.reference_code
    assert event["params"]["session_id"] == "1788681860"
    assert event["params"]["dispatch_channel"] == "server"
    assert "pat.completion@example.com" not in json.dumps(body)
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is not None  # the thank-you page stops offering the browser copy

    # The browser copy is now suppressed and the sweep does not send again.
    html = client.get(f"/enquiries/{booking.id}/thanks").text
    assert "function_enquiry_submitted" not in html
    conversions.run_sweep(db, now=created + dt.timedelta(hours=3))
    assert len([u for u, _ in post.calls if "google-analytics" in u]) == 1


def test_ga4_fallback_never_runs_beside_a_confirmed_browser_send(client, db, server_dispatch_on, monkeypatch):
    booking = _ga4_booking(client, db)
    assert client.post(f"/enquiries/{booking.id}/conversion/ga4").status_code == 204
    post = _Post({"google-analytics.com": httpx.Response(204), "graph.facebook.com": httpx.Response(200, json={"events_received": 1})})
    monkeypatch.setattr(httpx, "post", post)
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=2))
    assert not [u for u, _ in post.calls if "google-analytics" in u]


def test_ga4_fallback_is_skipped_without_a_client_id(client, db, server_dispatch_on, monkeypatch):
    resp = client.post("/enquiries", data=_payload())  # no _ga cookie at all
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    post = _Post({"google-analytics.com": httpx.Response(204), "graph.facebook.com": httpx.Response(200, json={"events_received": 1})})
    monkeypatch.setattr(httpx, "post", post)
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=2))
    row = db.query(ConversionDispatch).filter_by(booking_id=booking.id, platform=PLATFORM_GA4, channel=CHANNEL_SERVER).one()
    assert row.status == STATUS_SKIPPED and "client id" in row.last_error
    assert booking.ga4_conversion_dispatched_at is None  # the browser copy stays on offer


# --- environment controls ----------------------------------------------------------


def test_nothing_is_sent_and_nothing_is_marked_when_server_dispatch_is_off(client, db, monkeypatch):
    monkeypatch.setattr(settings, "tracking_server_dispatch_enabled", False)
    monkeypatch.setattr(settings, "meta_capi_access_token", "EAAtoken")
    monkeypatch.setattr(settings, "ga4_api_secret", "secret")
    booking = _ga4_booking(client, db)
    post = _Post({})
    monkeypatch.setattr(httpx, "post", post)
    conversions.dispatch_meta(db, booking)
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=2))
    assert post.calls == []
    assert {r.status for r in db.query(ConversionDispatch).filter_by(booking_id=booking.id)} == {STATUS_SKIPPED}
    assert booking.ga4_conversion_dispatched_at is None


def test_staff_bookings_never_get_a_server_copy(db, loft, server_dispatch_on, monkeypatch):
    from app.services.booking import create_booking
    contact = Contact(name="Phone Caller", email="phone.completion@example.com")
    db.add(contact)
    db.flush()
    staff = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 7, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Phoned In",
        event_type="birthday", adult_count=30, child_count=0, notes=None, actor="staff",
    )
    post = _Post({})
    monkeypatch.setattr(httpx, "post", post)
    row = conversions.dispatch_meta(db, staff)
    assert row.status == STATUS_SKIPPED and post.calls == []


def test_browser_beacon_writes_a_dispatch_row_once(client, db):
    booking = _public_booking(client, db)
    assert client.post(f"/enquiries/{booking.id}/conversion/meta").status_code == 204
    assert client.post(f"/enquiries/{booking.id}/conversion/meta").status_code == 204
    rows = db.query(ConversionDispatch).filter_by(booking_id=booking.id, platform=PLATFORM_META, channel=CHANNEL_BROWSER).all()
    assert len(rows) == 1 and rows[0].status == STATUS_SENT and rows[0].event_id == booking.reference_code


def test_the_admin_booking_page_lists_conversion_sends(admin_client, client, db):
    booking = _public_booking(client, db)
    client.post(f"/enquiries/{booking.id}/conversion/ga4")
    page = admin_client.get(f"/admin/bookings/{booking.id}").text
    assert "Conversion sends (1)" in page
    assert "browser" in page


def test_an_untagged_direct_return_is_still_the_last_touch():
    # The rule the existing enquiry test also pins: a visitor who came from
    # an ad three days ago and now returns by typing the address has a last
    # touch of "unknown"; the older click is not carried forward.
    first = attribution.build_touch({"gclid": "ADCLICK", "captured_at": "2026-09-03T10:00:00Z"})
    last = attribution.build_touch({"referrer": None, "captured_at": "2026-09-06T10:00:00Z"})
    f, l = attribution.reconcile_touches(first, last, attribution.touches_from_cookies({"_gcl_aw": "GCL.1788000000.ADCLICK"}))
    assert f["gclid"] == "ADCLICK"
    assert l["gclid"] is None and l["referrer_category"] == "unknown"


def test_the_internal_hop_is_not_a_touch_but_an_external_referral_is():
    hop = attribution.build_touch({"referrer": "https://meantime.com.au/", "captured_at": "2026-09-06T10:00:00Z"})
    blog = attribution.build_touch({"referrer": "https://some-wedding-blog.example/venues", "captured_at": "2026-09-06T11:00:00Z"})
    click = attribution.touches_from_cookies({"_gcl_aw": "GCL.1788000000.ADCLICK"})
    f, l = attribution.reconcile_touches(hop, hop, click)
    assert f["gclid"] == "ADCLICK" and l["gclid"] == "ADCLICK"
    f2, l2 = attribution.reconcile_touches(hop, blog, click)
    assert f2["gclid"] == "ADCLICK" and l2["referrer_category"] == "referral"
