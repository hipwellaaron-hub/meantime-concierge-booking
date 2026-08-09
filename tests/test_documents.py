import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.document import DocumentStatus, DocumentType
from app.services.document_generation import compute_food_order_total, generate_agreement_content, generate_beo_content
from app.services.documents import create_new_version, get_by_token, get_current, mark_sent, record_view, sign


def test_new_version_supersedes_not_overwrites(db, booking):
    v1 = create_new_version(db, booking, DocumentType.beo, {"n": 1}, actor="test")
    token_v1 = v1.access_token

    v2 = create_new_version(db, booking, DocumentType.beo, {"n": 2}, actor="test")

    assert v2.version == 2
    assert v1.version == 1
    assert v2.is_current is True

    # old row was neither deleted nor mutated
    old = get_by_token(db, token_v1)
    assert old is not None
    assert old.content == {"n": 1}
    assert old.is_current is False

    assert get_current(db, booking.id, DocumentType.beo).id == v2.id


def test_only_one_current_version_per_booking_and_type(db, booking):
    create_new_version(db, booking, DocumentType.beo, {"n": 1}, actor="test")
    create_new_version(db, booking, DocumentType.beo, {"n": 2}, actor="test")
    create_new_version(db, booking, DocumentType.beo, {"n": 3}, actor="test")

    current = get_current(db, booking.id, DocumentType.beo)
    assert current.version == 3


def test_agreement_and_beo_version_independently(db, booking):
    create_new_version(db, booking, DocumentType.beo, {}, actor="test")
    create_new_version(db, booking, DocumentType.agreement, {}, actor="test")

    assert get_current(db, booking.id, DocumentType.beo).version == 1
    assert get_current(db, booking.id, DocumentType.agreement).version == 1


def test_draft_document_is_not_publicly_viewable(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_full_lifecycle_view_then_sign(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="aaron@meantime.com.au")
    assert document.status == DocumentStatus.sent

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)

        resp = client.get(f"/d/{document.access_token}")
        assert resp.status_code == 200
        assert "Wilson Wedding" in resp.text
        db.refresh(document)
        assert document.status == DocumentStatus.viewed

        resp = client.post(f"/d/{document.access_token}/sign", data={"signer_name": "Pat Wilson"})
        assert resp.status_code in (200, 303)
        db.refresh(document)
        assert document.status == DocumentStatus.signed
        assert document.signer_name == "Pat Wilson"
        assert document.signed_at is not None
        assert document.signer_ip is not None
    finally:
        app.dependency_overrides.clear()


def test_cannot_sign_twice(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")
    sign(db, document, signer_name="Pat Wilson", signer_ip="1.2.3.4")

    with pytest.raises(ValueError):
        sign(db, document, signer_name="Someone Else", signer_ip="5.6.7.8")


def test_cannot_send_a_document_twice(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    mark_sent(db, document, actor="test")
    with pytest.raises(ValueError):
        mark_sent(db, document, actor="test")


def test_beo_generation_flags_missing_data_for_review(db, booking):
    content = generate_beo_content(booking)
    assert "[REVIEW]" in content["bar_structure"]
    assert "[REVIEW]" in content["food_order"]["note"]
    assert content["total_food_spend"]["total"] is None
    assert content["special_notes"] == "Bride requests no seafood."
    assert content["status"] == "enquiry"


def test_beo_generation_computes_food_total_when_line_items_given(db, booking):
    line_items = [
        {"item": "Antipasto platter", "quantity": 4, "unit_price": "65.00"},
        {"item": "Margherita pizza", "quantity": 10, "unit_price": "18.00"},
    ]
    content = generate_beo_content(booking, food_order_line_items=line_items)
    assert content["total_food_spend"]["total"] == "440.00"
    # deposit/balance still can't be known -- Phase 3 doesn't exist yet
    assert content["total_food_spend"]["deposit_paid"] is None
    assert "[REVIEW]" in content["total_food_spend"]["note"]


def test_compute_food_order_total_empty_is_none():
    assert compute_food_order_total([]) is None


def test_compute_food_order_total_malformed_line_item_raises_clean_error():
    with pytest.raises(ValueError):
        compute_food_order_total([{"item": "Pizza", "quantity": "not a number", "unit_price": "18.00"}])


def test_compute_food_order_total_missing_key_raises_clean_error():
    with pytest.raises(ValueError):
        compute_food_order_total([{"item": "Pizza", "quantity": 1}])  # no unit_price


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_oversized_signer_name_rejected(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(f"/d/{document.access_token}/sign", data={"signer_name": "x" * 300})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_huge_forwarded_for_header_does_not_crash_signing(db, booking):
    """Regression: Document.signer_ip is a 45-char column. A garbage or
    oversized X-Forwarded-For header used to be stored unbounded, which
    would crash the insert instead of just recording a truncated value."""
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(
            f"/d/{document.access_token}/sign",
            data={"signer_name": "Pat Wilson"},
            headers={"x-forwarded-for": "9" * 500},
        )
        assert resp.status_code in (200, 303)
        db.refresh(document)
        assert document.signer_ip is not None
        assert len(document.signer_ip) <= 45
    finally:
        app.dependency_overrides.clear()


def test_sign_rate_limit_blocks_after_threshold(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        statuses = []
        for _ in range(11):
            resp = client.post(f"/d/{document.access_token}/sign", data={"signer_name": "Pat Wilson"})
            statuses.append(resp.status_code)
        assert 429 in statuses
    finally:
        app.dependency_overrides.clear()


def test_garbage_post_to_sign_endpoint_returns_422_not_500(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(f"/d/{document.access_token}/sign", data={})  # no signer_name at all
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_null_byte_in_token_returns_404_not_500(db):
    """Regression: a URL-encoded null byte in the token path used to
    reach the database (Postgres text params reject NUL bytes outright)
    and crash with an unhandled 500 instead of a clean 404."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/d/token%00.txt")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_absurdly_long_token_returns_404_not_500(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{'a' * 5000}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_sign_unknown_token_returns_404_not_500(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/d/not-a-real-token/sign", data={"signer_name": "Pat Wilson"})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_signed_at_is_displayed_in_sydney_time_not_utc(db, booking):
    """Regression: signed_at is stored UTC-aware (correct for storage),
    but the template used to render it with a raw .strftime(), which
    prints whatever timezone the value happens to carry -- UTC. A client
    who signed at 10am Sydney on 7 March should never see '6 March'
    just because the server displayed UTC instead of local time."""
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")
    sign(db, document, signer_name="Pat Wilson", signer_ip="1.2.3.4")

    # 10:00am Sydney (UTC+11 in March, daylight saving) is 23:00 UTC the
    # previous day -- deliberately chosen to cross a calendar-date
    # boundary between UTC and Sydney local time.
    document.signed_at = dt.datetime(2027, 3, 6, 23, 0, tzinfo=dt.timezone.utc)
    db.add(document)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}")
        assert resp.status_code == 200
        assert "07 Mar 2027" in resp.text  # Sydney date
        assert "06 Mar 2027" not in resp.text  # UTC date must not leak through
    finally:
        app.dependency_overrides.clear()


def test_event_name_with_html_is_escaped_in_rendered_document(db, loft):
    """Security pass: content that flows into the public template
    (event_name, notes, etc. are all client-controlled at enquiry time)
    must be HTML-escaped, or a booking name like this becomes stored XSS
    against anyone who opens the document link."""
    from app.services.booking import create_booking as _create_booking

    malicious_booking = _create_booking(
        db,
        space_id=loft.id,
        contact_id=None,
        event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0),
        end_time=dt.time(17, 0),
        event_name="<script>alert('xss')</script>",
        event_type="party",
        adult_count=10,
        child_count=0,
        notes=None,
        actor="test",
    )
    document = create_new_version(
        db, malicious_booking, DocumentType.agreement, generate_agreement_content(malicious_booking), actor="test"
    )
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}")
        assert resp.status_code == 200
        assert "<script>alert" not in resp.text
        assert "&lt;script&gt;" in resp.text
    finally:
        app.dependency_overrides.clear()
