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


# --- the mt_touch_* contract: captured_at is required and must be real ---------
#
# The cookie contract (docs/tracking-handover.md 2.2) makes captured_at a
# required ISO 8601 field. These four tests are what "required" means:
# a cookie without a usable one is ignored rather than guessed at.


def test_a_site_touch_cookie_with_no_captured_at_is_not_a_touch():
    # Without the rule, build_touch stamps now() -- so mt_touch_first, the
    # OLDEST thing the website knows, would sort newest and take last touch
    # away from the campaign that actually brought the visitor back.
    cookies = {"mt_touch_first": _site_touch_cookie(utm_source="google", utm_medium="cpc")}
    assert attribution.touches_from_cookies(cookies) == []

    page_last = attribution.build_touch({"utm_source": "newsletter", "captured_at": "2026-09-06T10:00:00Z"})
    first, last = attribution.reconcile_touches(page_last, page_last, attribution.touches_from_cookies(cookies))
    assert last["utm_source"] == "newsletter"


def test_a_site_touch_cookie_with_an_unreadable_captured_at_is_not_a_touch():
    for bad in ("yesterday", "", "2026-13-45T99:99:99Z", 1788681860):
        cookies = {"mt_touch_last": _site_touch_cookie(utm_source="google", captured_at=bad)}
        assert attribution.touches_from_cookies(cookies) == [], bad


def test_a_captured_at_in_the_future_cannot_claim_last_touch():
    # These cookies are client-controlled and cost nothing to write. A
    # timestamp in 2030 would otherwise win last touch on every enquiry.
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365)).isoformat()
    cookies = {"mt_touch_last": _site_touch_cookie(utm_source="forged", captured_at=future)}
    assert attribution.touches_from_cookies(cookies) == []

    real = attribution.build_touch({"utm_source": "newsletter", "captured_at": "2026-09-06T10:00:00Z"})
    first, last = attribution.reconcile_touches(real, real, attribution.touches_from_cookies(cookies))
    assert last["utm_source"] == "newsletter"


def test_a_linker_cookie_dated_in_the_future_is_ignored_too():
    future_unix = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).timestamp())
    assert attribution.touches_from_cookies({"_gcl_aw": f"GCL.{future_unix}.FUTURECLICK"}) == []
    ahead_ms = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).timestamp() * 1000)
    assert attribution.touches_from_cookies({"_fbc": f"fb.1.{ahead_ms}.FUTUREFB"}) == []


def test_a_visitors_clock_running_an_hour_fast_is_still_a_real_visitor():
    # The future check is a forgery bound, not a clock-accuracy demand.
    skewed = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    cookies = {"mt_touch_last": _site_touch_cookie(utm_source="instagram", captured_at=skewed)}
    touches = attribution.touches_from_cookies(cookies)
    assert len(touches) == 1 and touches[0]["utm_source"] == "instagram"


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
    assert 'autocomplete="off"' in html and "pageshow" in html


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
    # TestClient's peer is the string "testclient", which is not an address,
    # so it is dropped rather than sent.
    assert event["user_data"] == {"client_user_agent": "Mozilla/5.0 test", "fbp": "fb.2.1.905305", "fbc": "fb.1.1788681860000.FBC"}
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
    post = _Post({"graph.facebook.com": httpx.Response(400, json={"error": {"message": "Invalid OAuth access token EAAtest-token-never-logged", "code": 190, "fbtrace_id": "Xy"}})})
    monkeypatch.setattr(httpx, "post", post)
    row = conversions.dispatch_meta(db, booking)
    assert row.status == STATUS_FAILED and row.attempts == 1
    assert row.next_attempt_at is not None
    # The provider's message text can echo request values; only codes are kept.
    assert row.last_error == "HTTP 400 code=190 fbtrace_id=Xy"
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
    # Accepted stays accepted: another sweep sends nothing more to Meta.
    conversions.run_sweep(db, now=now + dt.timedelta(hours=5))
    assert len([u for u, _ in post.calls if "graph.facebook.com" in u]) == 2


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
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is None  # the browser copy stays on offer


# --- environment controls ----------------------------------------------------------


def test_nothing_is_sent_and_nothing_is_recorded_when_the_opt_in_is_off(client, db, server_dispatch_on, monkeypatch):
    # Ids and secrets all configured; only the flag is off. Nothing goes
    # out and no row is written, so the enquiry is still sent once the
    # flag is set (review: a "skipped" row here was terminal).
    monkeypatch.setattr(settings, "tracking_server_dispatch_enabled", False)
    booking = _ga4_booking(client, db)
    post = _Post({})
    monkeypatch.setattr(httpx, "post", post)
    assert conversions.dispatch_meta(db, booking) is None
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=2))
    assert post.calls == []
    assert db.query(ConversionDispatch).filter_by(booking_id=booking.id).count() == 0
    assert booking.ga4_conversion_dispatched_at is None


def test_a_non_production_railway_environment_sends_nothing_even_with_the_flag(client, db, server_dispatch_on, monkeypatch):
    monkeypatch.setattr(settings, "railway_environment_name", "staging")
    booking = _ga4_booking(client, db)
    post = _Post({})
    monkeypatch.setattr(httpx, "post", post)
    assert conversions.dispatch_meta(db, booking) is None
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=2))
    assert post.calls == []


def test_an_enquiry_that_arrived_before_the_variables_is_sent_once_they_exist(client, db, server_dispatch_on, monkeypatch):
    monkeypatch.setattr(settings, "meta_capi_access_token", "")
    booking = _public_booking(client, db)
    assert conversions.dispatch_meta(db, booking) is None
    monkeypatch.setattr(settings, "meta_capi_access_token", "EAAnow-set")
    post = _Post({"graph.facebook.com": httpx.Response(200, json={"events_received": 1, "fbtrace_id": "late"})})
    monkeypatch.setattr(httpx, "post", post)
    summary = conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=1))
    assert summary["meta_first_send"] == 1 and summary["meta_accepted"] == 1


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


# --- review fixes -------------------------------------------------------------------


@pytest.mark.parametrize("cookies", [
    {"_gcl_aw": "GCL.99999999999999999999.x"},
    {"_fbc": "fb.1.99999999999999999999999999.x"},
    {"_gcl_aw": "GCL." + "1" * 4400 + ".x"},
    {"mt_touch_last": "AAAA" * 2000},
    {"_fbp": "x" * 5000},
])
def test_a_crafted_parent_domain_cookie_never_blocks_the_enquiry(client, db, cookies):
    resp = client.post("/enquiries", data=_payload(), cookies=cookies)
    assert resp.status_code == 303
    assert db.query(Booking).filter_by(event_name="Completion Enquiry").count() == 1


def test_non_ascii_cookie_digits_are_not_a_timestamp():
    # U+00B2 passes str.isdigit() and fails int(); Starlette hands cookie
    # bytes through as latin-1, so it reaches the parser (review). The
    # HTTP client used in tests refuses to send it, so the parser is
    # exercised directly.
    assert attribution.touches_from_cookies({"_gcl_aw": "GCL.\u00b2.x", "_fbc": "fb.1.\u00b2.x"}) == []
    context = conversions.build_tracking_context(
        cookies={"_ga": "\u00b2\u00b2", "_fbp": "\u00b2"}, user_agent=None, client_ip=None
    )
    assert "ga_client_id" not in context and "fbp" not in context


def test_an_unreadable_timestamp_is_left_out_of_the_ordering():
    first = attribution.build_touch({"gclid": "A", "captured_at": "2026-09-01T00:00:00Z"})
    garbage = attribution.build_touch({"utm_source": "x", "captured_at": "06/09/2026"})
    f, l = attribution.reconcile_touches(first, garbage, [])
    assert f["gclid"] == "A" and l["gclid"] == "A"


def test_cookie_touches_carry_their_provenance(client, db):
    client.post("/enquiries", data=_payload(), cookies={"_gcl_aw": "GCL.1788681860.PROV"})
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert booking.first_touch_attribution["source"] == "cookie:_gcl_aw"


def test_the_same_submission_id_with_different_content_is_a_new_lead(client, db):
    # Back from the thank-you page, change the date, submit: a new enquiry,
    # not the old one handed back (review).
    sid = str(uuid.uuid4())
    first = client.post("/enquiries", data={**_payload(), "submission_id": sid})
    second = client.post("/enquiries", data={**_payload(event_date="2027-11-21"), "submission_id": sid})
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] != second.headers["location"]
    rows = db.query(Booking).filter_by(event_name="Completion Enquiry").order_by(Booking.created_at).all()
    assert len(rows) == 2
    assert str(rows[0].submission_id) == sid and rows[1].submission_id is None


def test_the_same_submission_id_under_a_different_email_is_a_new_lead(client, db, monkeypatch):
    from app.services import enquiry_classification
    monkeypatch.setattr(enquiry_classification, "DUPLICATE_SUBMISSION_WINDOW", dt.timedelta(seconds=0))
    sid = str(uuid.uuid4())
    client.post("/enquiries", data={**_payload(), "submission_id": sid})
    resp = client.post("/enquiries", data={**_payload(email="someone.else@example.com"), "submission_id": sid})
    assert resp.status_code == 303
    assert db.query(Booking).filter_by(event_name="Completion Enquiry").count() == 2


def test_the_submission_id_is_written_in_the_same_insert_as_the_booking(client, db):
    # No second transaction: the id is on the row from its first commit.
    from sqlalchemy import event as sa_event
    from app.models import Booking as B
    seen = {}

    @sa_event.listens_for(B, "after_insert")
    def _capture(mapper, connection, target):
        seen["submission_id"] = target.submission_id
        seen["tracking_context"] = target.tracking_context

    try:
        sid = str(uuid.uuid4())
        client.post("/enquiries", data={**_payload(), "submission_id": sid}, cookies={"_ga": "GA1.1.5.6"})
    finally:
        sa_event.remove(B, "after_insert", _capture)
    assert str(seen["submission_id"]) == sid
    assert seen["tracking_context"]["ga_client_id"] == "5.6"


def test_free_text_event_type_never_reaches_a_payload(client, db, server_dispatch_on, monkeypatch):
    resp = client.post("/enquiries", data=_payload(event_type="Jane Smith's 40th jane@x.com"),
                       cookies={"_ga": "GA1.1.7.8"})
    assert resp.status_code == 303
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert conversions.meta_payload(booking)["data"][0]["custom_data"]["content_category"] == "other"
    assert conversions.ga4_payload(booking)["events"][0]["params"]["enquiry_type"] == "other"
    html = client.get(f"/enquiries/{booking.id}/thanks").text
    assert "jane@x.com" not in html


def test_a_forged_forwarded_address_is_not_stored(client, db, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    client.post("/enquiries", data=_payload(), headers={"x-forwarded-for": "<script>alert(1)</script>, 203.0.113.9"})
    booking = db.query(Booking).filter_by(event_name="Completion Enquiry").one()
    assert booking.tracking_context.get("client_ip") == "203.0.113.9"


def test_the_ga4_fallback_claims_the_send_before_it_posts(client, db, server_dispatch_on, monkeypatch):
    booking = _ga4_booking(client, db)
    state = {}

    def observing_post(url, **kwargs):
        if "google-analytics" in url:
            db.expire(booking)
            state["claimed_before_post"] = booking.ga4_conversion_dispatched_at is not None
            return httpx.Response(204)
        return httpx.Response(200, json={"events_received": 1})

    monkeypatch.setattr(httpx, "post", observing_post)
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=1))
    assert state["claimed_before_post"] is True


def test_a_failed_ga4_post_releases_the_claim(client, db, server_dispatch_on, monkeypatch):
    booking = _ga4_booking(client, db)
    post = _Post({"google-analytics.com": httpx.Response(503), "graph.facebook.com": httpx.Response(200, json={"events_received": 1})})
    monkeypatch.setattr(httpx, "post", post)
    conversions.run_sweep(db, now=booking.created_at + dt.timedelta(hours=1))
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is None  # the browser copy is on offer again
    row = db.query(ConversionDispatch).filter_by(booking_id=booking.id, platform=PLATFORM_GA4, channel=CHANNEL_SERVER).one()
    assert row.status == STATUS_FAILED


def test_one_bookings_failure_does_not_undo_anothers_recorded_send(client, db, server_dispatch_on, monkeypatch):
    from sqlalchemy import text
    a = _public_booking(client, db)
    b_resp = client.post("/enquiries", data=_payload(email="second.completion@example.com", event_name="Second Completion"))
    assert b_resp.status_code == 303
    b = db.query(Booking).filter_by(event_name="Second Completion").one()
    calls = {"n": 0}

    # Fail on the SECOND BOOKING, not on the second call. The sweep orders
    # by created_at, and both bookings here have the same one: Postgres
    # now() is the transaction timestamp and the test fixture is one
    # transaction. So the tie is real and the order arbitrary -- keying on
    # the call number made this test pass or fail on row layout.
    def post(url, **kwargs):
        calls["n"] += 1
        if kwargs["json"]["data"][0]["event_id"] == b.reference_code:
            raise RuntimeError("provider library exploded")  # not an httpx error: the sweep must still survive
        return httpx.Response(200, json={"events_received": 1, "fbtrace_id": "first"})

    monkeypatch.setattr(httpx, "post", post)
    summary = conversions.run_sweep(db, now=a.created_at + dt.timedelta(minutes=1))
    assert summary["errors"] == 1 and summary["meta_accepted"] == 1
    first_row = db.query(ConversionDispatch).filter_by(booking_id=a.id, platform=PLATFORM_META, channel=CHANNEL_SERVER).one()
    assert first_row.status == STATUS_ACCEPTED
    assert db.execute(text("select count(*) from conversion_dispatches where booking_id=:b"), {"b": b.id}).scalar() == 0


def test_two_beacons_for_one_platform_do_not_error(client, db):
    booking = _public_booking(client, db)
    # Simulate the losing side of a concurrent double: a row already exists
    # by the time this call looks. The insert must not raise.
    conversions.record_browser_dispatch(db, booking, PLATFORM_GA4)
    db.expire_all()
    row = conversions.record_browser_dispatch(db, booking, PLATFORM_GA4)
    assert row.status == STATUS_SENT
    assert db.query(ConversionDispatch).filter_by(booking_id=booking.id, platform=PLATFORM_GA4).count() == 1


def test_the_secret_bearing_request_log_is_silenced():
    import logging
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_the_address_and_user_agent_are_retired_after_the_windows(client, db, server_dispatch_on, monkeypatch):
    from sqlalchemy import text
    booking = _public_booking(client, db)
    assert "user_agent" in booking.tracking_context
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
    db.execute(text("update bookings set created_at=:t where id=:i"), {"t": old, "i": booking.id})
    db.commit()
    post = _Post({})
    monkeypatch.setattr(httpx, "post", post)
    summary = conversions.run_sweep(db)
    db.refresh(booking)
    assert summary["context_retired"] == 1
    assert "user_agent" not in booking.tracking_context and "client_ip" not in booking.tracking_context
    assert booking.tracking_context.get("fbp") == "fb.2.1.905305"  # the pseudonymous ids stay
    assert post.calls == []  # nothing that old is sent


def test_validate_ga4_payload_writes_nothing(client, db, server_dispatch_on, monkeypatch):
    booking = _ga4_booking(client, db)
    monkeypatch.setattr(httpx, "post", _Post({"debug/mp/collect": httpx.Response(200, json={"validationMessages": []})}))
    assert conversions.validate_ga4_payload(booking) == []
    assert db.query(ConversionDispatch).filter_by(booking_id=booking.id).count() == 0
    db.refresh(booking)
    assert booking.ga4_conversion_dispatched_at is None
