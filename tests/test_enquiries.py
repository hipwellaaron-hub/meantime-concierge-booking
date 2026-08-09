import datetime as dt

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, Contact
from app.services.contact_matching import find_or_create_contact
from app.services.lead_analytics import classify_lead_source, get_lead_source_breakdown


def _payload(**overrides):
    payload = dict(
        name="Pat Wilson",
        email="pat@example.com",
        phone="0400000000",
        event_name="Wilson Wedding",
        event_date="2026-11-14",
        attendee_count=80,
        event_type="wedding",
        proposed_time_slot="Saturday evening",
        comments="Would love a look at the space first.",
    )
    payload.update(overrides)
    return payload


def test_submit_enquiry_creates_contact_and_booking(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id)))
        assert resp.status_code == 201
        body = resp.json()
        assert body["reference_code"].startswith("HAM-20261114-")
        assert body["possible_duplicate_contact"] is False

        booking = db.get(Booking, body["booking_id"])
        assert booking.status.value == "enquiry"
        assert booking.proposed_time_slot == "Saturday evening"
        assert booking.start_time is None
        assert booking.end_time is None
        assert booking.adult_count == 80
        assert booking.event_name == "Wilson Wedding"

        contact = db.get(Contact, booking.contact_id)
        assert contact.email == "pat@example.com"
    finally:
        app.dependency_overrides.clear()


def test_two_enquiries_for_same_slot_both_succeed(db, loft):
    """This is the actual point of lead capture: a second enquiry for a
    date someone already enquired about must not be rejected."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        r1 = client.post("/enquiries", json=_payload(space_id=str(loft.id), email="a@example.com"))
        r2 = client.post("/enquiries", json=_payload(space_id=str(loft.id), email="b@example.com"))
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["booking_id"] != r2.json()["booking_id"]
    finally:
        app.dependency_overrides.clear()


def test_repeat_enquiry_same_email_reuses_contact(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        r1 = client.post("/enquiries", json=_payload(space_id=str(loft.id)))
        r2 = client.post("/enquiries", json=_payload(space_id=str(loft.id), event_name="Wilson Wedding (follow-up)"))
        assert r1.json()["contact_id"] == r2.json()["contact_id"]
    finally:
        app.dependency_overrides.clear()


def test_unknown_space_returns_422(db, loft):
    import uuid

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(uuid.uuid4())))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_find_or_create_contact_reuses_on_exact_email(db):
    contact_a, _ = find_or_create_contact(db, "Jane Smith", "jane@example.com", None)
    contact_b, dupes = find_or_create_contact(db, "Jane S.", "JANE@example.com", None)
    assert contact_a.id == contact_b.id
    assert dupes == []


def test_find_or_create_contact_surfaces_but_does_not_merge_fuzzy_match(db):
    contact_a, _ = find_or_create_contact(db, "Jane Smith", "jane@work.com", None)
    contact_b, dupes = find_or_create_contact(db, "Jane Smith", "jane@personal.com", None)
    assert contact_a.id != contact_b.id  # never auto-merged
    assert len(dupes) == 1
    assert dupes[0].contact.id == contact_a.id


def test_classify_lead_source_prefers_explicit_value():
    assert classify_lead_source("google", "https://ivvy.com.au/somewhere") == "google"


def test_classify_lead_source_detects_ivvy_marketplace_referrer():
    assert classify_lead_source(None, "https://www.ivvy.com.au/venue/hamilton") == "ivvy_marketplace"


def test_classify_lead_source_detects_google_referrer():
    assert classify_lead_source(None, "https://www.google.com/search?q=hamilton+function+venue") == "google"


def test_classify_lead_source_defaults_to_direct_when_no_referrer():
    assert classify_lead_source(None, None) == "direct"


def test_classify_lead_source_falls_back_to_referral():
    assert classify_lead_source(None, "https://some-wedding-blog.example/best-venues") == "referral"


def test_lead_source_breakdown_counts_by_source(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.post("/enquiries", json=_payload(space_id=str(loft.id), email="a@example.com", lead_source="google"))
        client.post("/enquiries", json=_payload(space_id=str(loft.id), email="b@example.com", lead_source="google"))
        client.post("/enquiries", json=_payload(space_id=str(loft.id), email="c@example.com", lead_source="ivvy_marketplace"))
    finally:
        app.dependency_overrides.clear()

    breakdown = get_lead_source_breakdown(db)
    assert breakdown["google"] == 2
    assert breakdown["ivvy_marketplace"] == 1


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_very_long_but_valid_email_does_not_crash(db, loft):
    """Regression: BookingEvent.actor is a 255-char column, and
    f"public_enquiry:{email}" used to be built without truncation. A
    long-but-RFC-valid email (email-validator allows up to ~252 chars
    total) pushed the combined string past 255 and crashed the insert
    with an unhandled DataError (500)."""
    long_email = f"{'a' * 240}@example.com"
    assert len(long_email) > 240

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), email=long_email))
        assert resp.status_code == 201
    finally:
        app.dependency_overrides.clear()


def test_double_submission_returns_same_booking_not_a_duplicate(db, loft):
    """Regression: a double-clicked submit (or a client retry) used to
    create two separate enquiry bookings with two reference codes."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        payload = _payload(space_id=str(loft.id), email="doubleclick@example.com")
        r1 = client.post("/enquiries", json=payload)
        r2 = client.post("/enquiries", json=payload)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["booking_id"] == r2.json()["booking_id"]
        assert r1.json()["reference_code"] == r2.json()["reference_code"]

        count = db.query(Booking).filter_by(event_name="Wilson Wedding").count()
        assert count == 1
    finally:
        app.dependency_overrides.clear()


def test_genuinely_different_enquiry_shortly_after_is_not_treated_as_duplicate(db, loft):
    """A different event name from the same contact for the same date
    must still go through -- the duplicate guard keys on more than just
    contact+date so a genuine second, different enquiry isn't swallowed."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        r1 = client.post(
            "/enquiries", json=_payload(space_id=str(loft.id), email="same@example.com", event_name="Wilson Wedding")
        )
        r2 = client.post(
            "/enquiries",
            json=_payload(space_id=str(loft.id), email="same@example.com", event_name="Wilson Wedding Reception"),
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["booking_id"] != r2.json()["booking_id"]
    finally:
        app.dependency_overrides.clear()


def test_enquiry_rate_limit_blocks_after_threshold(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        statuses = []
        for i in range(6):
            resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), email=f"flood{i}@example.com"))
            statuses.append(resp.status_code)
        assert statuses[:5] == [201] * 5
        assert statuses[5] == 429
    finally:
        app.dependency_overrides.clear()


def test_blank_name_after_strip_is_rejected(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), name="   "))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_oversized_comments_rejected(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), comments="x" * 5001))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_absurd_attendee_count_rejected(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), attendee_count=10_000_000))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_zero_attendee_count_rejected(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), attendee_count=0))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_unicode_name_is_accepted_and_stored_correctly(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        unicode_name = "José García 🎉 李明"
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), name=unicode_name, email="unicode@example.com"))
        assert resp.status_code == 201
        booking = db.get(Booking, resp.json()["booking_id"])
        contact = db.get(Contact, booking.contact_id)
        assert contact.name == unicode_name
    finally:
        app.dependency_overrides.clear()


def test_sql_injection_style_name_is_stored_inert_not_executed(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        payload_name = "Robert'); DROP TABLE bookings;--"
        resp = client.post("/enquiries", json=_payload(space_id=str(loft.id), name=payload_name, email="sqli@example.com"))
        assert resp.status_code == 201
        booking = db.get(Booking, resp.json()["booking_id"])
        contact = db.get(Contact, booking.contact_id)
        assert contact.name == payload_name  # stored verbatim as inert data

        # the bookings table must still exist and be queryable
        assert db.query(Booking).count() >= 1
    finally:
        app.dependency_overrides.clear()


def test_malformed_json_body_returns_422_not_500(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", content=b"not json at all {{{", headers={"content-type": "application/json"})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_missing_required_fields_returns_422(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json={"name": "Someone"})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_wrong_field_types_returns_422(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(
            "/enquiries",
            json=_payload(space_id=str(loft.id), attendee_count="not a number", event_date="not a date"),
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_oversized_request_body_rejected(db, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        huge_payload = _payload(space_id=str(loft.id), comments="x" * 300_000)
        resp = client.post("/enquiries", json=huge_payload)
        assert resp.status_code in (413, 422)
    finally:
        app.dependency_overrides.clear()


def test_non_bookable_space_rejected(db, hamilton, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/enquiries", json=_payload(space_id=str(unassigned_space.id)))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()
