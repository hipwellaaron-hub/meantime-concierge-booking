import datetime as dt

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Booking, BookingEvent, Contact
from app.services.contact_matching import find_or_create_contact
from app.services.lead_analytics import classify_lead_source, get_lead_source_breakdown


def _payload(**overrides):
    payload = dict(
        first_name="Pat",
        last_name="Wilson",
        email="pat@example.com",
        phone="0400000000",
        event_name="Wilson Wedding",
        event_date="2026-11-14",
        dates_flexible="false",
        event_type="Wedding",
        attendee_count=80,
        proposed_time_slot="Saturday evening",
        comments="Would love a look at the space first.",
    )
    payload.update(overrides)
    return payload


def test_submit_enquiry_creates_contact_and_booking(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload())
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/enquiries/")

        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        assert booking.status.value == "enquiry"
        assert booking.space_id == unassigned_space.id
        assert booking.proposed_time_slot == "Saturday evening"
        assert booking.start_time is None
        assert booking.end_time is None
        assert booking.adult_count == 80
        assert booking.child_count == 0

        contact = db.get(Contact, booking.contact_id)
        assert contact.name == "Pat Wilson"
        assert contact.email == "pat@example.com"
    finally:
        app.dependency_overrides.clear()


def test_blank_adult_count_from_a_real_form_submission_is_treated_as_not_provided(db, unassigned_space):
    """Regression: a real browser submits an empty <input type=number> as
    the literal string "", not an omitted field -- int("") used to crash
    Pydantic's coercion with an unhandled 422 that had nothing to do with
    the field actually being invalid."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(adult_count=""))
        assert resp.status_code == 303
        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        assert booking.adult_count == 80  # falls back to attendee_count, not provided
    finally:
        app.dependency_overrides.clear()


def test_adult_count_splits_into_adult_and_child_count(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(attendee_count=70, adult_count=65))
        assert resp.status_code == 303

        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        assert booking.adult_count == 65
        assert booking.child_count == 5
    finally:
        app.dependency_overrides.clear()


def test_company_and_dates_flexible_fold_into_notes(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/enquiries", data=_payload(company_name="Acme Pty Ltd", dates_flexible="true", comments="Weekday ok too")
        )
        assert resp.status_code == 303

        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        assert "Acme Pty Ltd" in booking.notes
        assert "Dates flexible: yes" in booking.notes
        assert "Weekday ok too" in booking.notes
    finally:
        app.dependency_overrides.clear()


def test_two_enquiries_for_same_slot_both_succeed(db, unassigned_space):
    """This is the actual point of lead capture: a second enquiry for a
    date someone already enquired about must not be rejected."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        r1 = client.post("/enquiries", data=_payload(email="a@example.com"))
        r2 = client.post("/enquiries", data=_payload(email="b@example.com"))
        assert r1.status_code == 303
        assert r2.status_code == 303
        assert r1.headers["location"] != r2.headers["location"]
    finally:
        app.dependency_overrides.clear()


def test_repeat_enquiry_same_email_reuses_contact(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        client.post("/enquiries", data=_payload())
        client.post("/enquiries", data=_payload(event_name="Wilson Wedding (follow-up)"))

        contacts = db.query(Contact).filter_by(email="pat@example.com").all()
        assert len(contacts) == 1
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


def test_possible_duplicate_contact_is_flagged_on_the_booking(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        client.post("/enquiries", data=_payload(email="jane@work.com", first_name="Jane", last_name="Smith"))
        client.post(
            "/enquiries",
            data=_payload(email="jane@personal.com", first_name="Jane", last_name="Smith", event_name="Smith Party"),
        )

        booking = db.query(Booking).filter_by(event_name="Smith Party").one()
        flags = db.query(BookingEvent).filter_by(booking_id=booking.id, event_type="enquiry_flagged").all()
        assert any("similar to an existing contact" in f.new_value for f in flags)
    finally:
        app.dependency_overrides.clear()


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


def test_lead_source_breakdown_counts_by_source(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        client.post("/enquiries", data=_payload(email="a@example.com", lead_source="google"))
        client.post("/enquiries", data=_payload(email="b@example.com", lead_source="google"))
        client.post("/enquiries", data=_payload(email="c@example.com", lead_source="ivvy_marketplace"))
    finally:
        app.dependency_overrides.clear()

    breakdown = get_lead_source_breakdown(db)
    assert breakdown["google"] == 2
    assert breakdown["ivvy_marketplace"] == 1


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_very_long_but_valid_email_does_not_crash(db, unassigned_space):
    """Regression: BookingEvent.actor is a 255-char column, and
    f"public_enquiry:{email}" used to be built without truncation. A
    long-but-RFC-valid email (email-validator allows up to ~252 chars
    total) pushed the combined string past 255 and crashed the insert
    with an unhandled DataError (500)."""
    long_email = f"{'a' * 240}@example.com"
    assert len(long_email) > 240

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(email=long_email))
        assert resp.status_code == 303
    finally:
        app.dependency_overrides.clear()


def test_double_submission_returns_same_booking_not_a_duplicate(db, unassigned_space):
    """Regression: a double-clicked submit (or a client retry) used to
    create two separate enquiry bookings with two reference codes."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        payload = _payload(email="doubleclick@example.com")
        r1 = client.post("/enquiries", data=payload)
        r2 = client.post("/enquiries", data=payload)
        assert r1.status_code == 303
        assert r2.status_code == 303
        assert r1.headers["location"] == r2.headers["location"]

        count = db.query(Booking).filter_by(event_name="Wilson Wedding").count()
        assert count == 1
    finally:
        app.dependency_overrides.clear()


def test_genuinely_different_enquiry_shortly_after_is_not_treated_as_duplicate(db, unassigned_space):
    """A different event name from the same contact for the same date
    must still go through -- the duplicate guard keys on more than just
    contact+date so a genuine second, different enquiry isn't swallowed."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        r1 = client.post("/enquiries", data=_payload(email="same@example.com", event_name="Wilson Wedding"))
        r2 = client.post(
            "/enquiries", data=_payload(email="same@example.com", event_name="Wilson Wedding Reception")
        )
        assert r1.status_code == 303
        assert r2.status_code == 303
        assert r1.headers["location"] != r2.headers["location"]
    finally:
        app.dependency_overrides.clear()


def test_enquiry_rate_limit_blocks_after_threshold(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        statuses = []
        for i in range(6):
            resp = client.post("/enquiries", data=_payload(email=f"flood{i}@example.com"))
            statuses.append(resp.status_code)
        assert statuses[:5] == [303] * 5
        assert statuses[5] == 429
    finally:
        app.dependency_overrides.clear()


def test_blank_name_after_strip_is_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(first_name="   "))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_oversized_comments_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(comments="x" * 5001))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_absurd_attendee_count_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(attendee_count=10_000_000))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_zero_attendee_count_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(attendee_count=0))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_adult_count_greater_than_attendee_count_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(attendee_count=10, adult_count=20))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_unknown_event_type_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data=_payload(event_type="Bar Mitzvah"))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_unicode_name_is_accepted_and_stored_correctly(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/enquiries",
            data=_payload(first_name="José", last_name="García 🎉 李明", email="unicode@example.com"),
        )
        assert resp.status_code == 303
        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        contact = db.get(Contact, booking.contact_id)
        assert contact.name == "José García 🎉 李明"
    finally:
        app.dependency_overrides.clear()


def test_sql_injection_style_name_is_stored_inert_not_executed(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        payload_name = "Robert'); DROP TABLE bookings;--"
        resp = client.post("/enquiries", data=_payload(first_name=payload_name, email="sqli@example.com"))
        assert resp.status_code == 303
        booking = db.query(Booking).filter_by(event_name="Wilson Wedding").one()
        contact = db.get(Contact, booking.contact_id)
        assert contact.name.startswith(payload_name)  # stored verbatim as inert data

        # the bookings table must still exist and be queryable
        assert db.query(Booking).count() >= 1
    finally:
        app.dependency_overrides.clear()


def test_missing_required_fields_returns_422(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/enquiries", data={"first_name": "Someone"})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_wrong_field_types_returns_422(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/enquiries", data=_payload(attendee_count="not a number", event_date="not a date")
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_oversized_request_body_rejected(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        huge_payload = _payload(comments="x" * 300_000)
        resp = client.post("/enquiries", data=huge_payload)
        assert resp.status_code in (413, 422)
    finally:
        app.dependency_overrides.clear()


def test_enquiry_form_renders(db, unassigned_space):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/enquire")
        assert resp.status_code == 200
        assert "Event Type" in resp.text
        assert "Wedding" in resp.text
    finally:
        app.dependency_overrides.clear()
