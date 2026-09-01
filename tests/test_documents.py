import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.document import DocumentStatus, DocumentType
from app.services.document_generation import (
    compute_food_order_total,
    generate_agreement_content,
    generate_beo_content,
    rebuild_terms_text,
)
from app.services.documents import (
    create_new_version,
    delete_draft,
    get_by_token,
    get_current,
    mark_sent,
    record_view,
    sign,
    update_content,
)


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


def test_deposit_clause_matches_invoice_due_on_issue(db, booking):
    # The deposit invoice is due on issue; the agreement must not tell the
    # client they have 7 days, or the two documents contradict each other.
    terms = generate_agreement_content(booking)["terms_text"]
    assert "payable on issue of the deposit invoice" in terms
    assert "within 7 days of making your booking" not in terms


def test_trading_hours_clause_covers_functions_to_midnight(db, booking):
    # Restaurant trading hours (12pm-9pm Wed/Thu) must not read as the
    # client's function curfew -- functions are licensed to midnight.
    terms = generate_agreement_content(booking)["terms_text"]
    assert "licensed to run until midnight" in terms
    assert "music off by 11:30pm" in terms
    assert "12pm to 9pm" not in terms


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
        assert document.viewed_at is not None

        first_viewed_at = document.viewed_at
        client.get(f"/d/{document.access_token}")  # a second visit must not move the timestamp
        db.refresh(document)
        assert document.viewed_at == first_viewed_at

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


# --- email-validity guard on the send path --------------------------------


def test_cannot_send_document_when_booking_has_no_contact(db, loft):
    from app.services.booking import create_booking as _create_booking

    contactless_booking = _create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="No Contact Booking",
        event_type="party", adult_count=10, child_count=0, notes=None, actor="test",
    )
    document = create_new_version(
        db, contactless_booking, DocumentType.agreement, generate_agreement_content(contactless_booking), actor="test"
    )
    with pytest.raises(ValueError, match="valid email"):
        mark_sent(db, document, actor="test")
    assert document.status == DocumentStatus.draft  # refused, not silently "sent"


def test_cannot_send_document_when_contact_email_is_malformed(db, loft):
    from app.models import Contact
    from app.services.booking import create_booking as _create_booking

    bad_contact = Contact(name="Bad Email Contact", email="not-an-email")
    db.add(bad_contact)
    db.flush()
    bad_booking = _create_booking(
        db, space_id=loft.id, contact_id=bad_contact.id, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="Bad Email Booking",
        event_type="party", adult_count=10, child_count=0, notes=None, actor="test",
    )
    document = create_new_version(
        db, bad_booking, DocumentType.agreement, generate_agreement_content(bad_booking), actor="test"
    )
    with pytest.raises(ValueError, match="valid email"):
        mark_sent(db, document, actor="test")


def test_beo_generation_flags_missing_data_for_review(db, booking):
    content = generate_beo_content(booking)
    assert "[REVIEW]" in content["bar_structure"]
    assert "[REVIEW]" in content["food_order"]["note"]
    assert "[REVIEW]" in content["event_timeline"]["notes"]
    assert content["total_food_spend"]["total"] is None
    assert content["special_notes"] == "Bride requests no seafood."
    assert content["status"] == "enquiry"


def test_beo_event_timeline_uses_real_setup_and_food_service_times_when_known(db, booking):
    booking.setup_access_time = dt.time(14, 0)
    booking.setup_access_confirmed = True
    booking.food_service_time = dt.time(18, 30)
    db.commit()

    content = generate_beo_content(booking)
    notes = content["event_timeline"]["notes"]
    assert "Setup access from 14:00 (confirmed)" in notes
    assert "Food service from 18:30" in notes
    assert "[REVIEW]" not in notes


def test_beo_event_timeline_notes_partial_when_only_one_time_known(db, booking):
    booking.food_service_time = dt.time(18, 30)
    db.commit()

    content = generate_beo_content(booking)
    notes = content["event_timeline"]["notes"]
    assert "Food service from 18:30" in notes
    assert "Setup access" not in notes
    assert "[REVIEW]" not in notes


def test_beo_event_timeline_shows_pending_when_setup_access_not_yet_confirmed(db, booking):
    booking.setup_access_time = dt.time(11, 0)
    booking.setup_access_confirmed = False
    db.commit()

    content = generate_beo_content(booking)
    assert "requested, pending confirmation" in content["event_timeline"]["notes"]


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
    from app.models import Contact
    from app.services.booking import create_booking as _create_booking

    xss_contact = Contact(name="XSS Test Contact", email="xss-test@example.com")
    db.add(xss_contact)
    db.flush()
    malicious_booking = _create_booking(
        db,
        space_id=loft.id,
        contact_id=xss_contact.id,
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


# --- real Master Policy terms content ----------------------------------------


def _booking_in(db, space, *, event_type=None, event_name="Test Event", notes=None, adult_count=70):
    from app.services.booking import create_booking as _create_booking

    return _create_booking(
        db, space_id=space.id, contact_id=None, event_date=dt.date(2027, 6, 5),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=event_name,
        event_type=event_type, adult_count=adult_count, child_count=0, notes=notes, actor="test",
    )


def test_loft_terms_use_loft_guest_numbers_and_minimum_spend(db, loft):
    booking = _booking_in(db, loft)
    content = generate_agreement_content(booking)
    assert "The Loft has a minimum requirement of 60 guests" in content["terms_text"]
    assert "$1,000 minimum spend" in content["terms_text"]
    assert "make up part of this total" in content["terms_text"]


def test_mezzanine_terms_use_mezzanine_guest_numbers_and_minimum_spend(db, mezzanine):
    booking = _booking_in(db, mezzanine)
    content = generate_agreement_content(booking)
    assert "The Mezzanine has a minimum requirement of 40 guests" in content["terms_text"]
    assert "$500 minimum spend" in content["terms_text"]
    assert "make up this total" in content["terms_text"]


def test_guest_numbers_clause_reflects_agreed_minimum_not_space_standard(db, loft):
    from app.services.booking import set_agreed_minimum
    from app.models.booking import MinReductionReasonCode

    booking = _booking_in(db, loft)
    set_agreed_minimum(db, booking, agreed_min_adults=40, reason=MinReductionReasonCode.aaron_discretion, actor="test")
    content = generate_agreement_content(booking)
    assert "minimum requirement of 40 guests" in content["terms_text"]
    assert "minimum requirement of 60 guests" not in content["terms_text"]
    # worked example must recompute too, never a stale literal
    assert "only 30 guests attend" in content["terms_text"]


def test_guest_numbers_clause_omits_worked_example_when_minimum_too_low_for_it(db, loft):
    from app.services.booking import set_agreed_minimum
    from app.models.booking import MinReductionReasonCode

    booking = _booking_in(db, loft)
    set_agreed_minimum(db, booking, agreed_min_adults=5, reason=MinReductionReasonCode.aaron_discretion, actor="test")
    content = generate_agreement_content(booking)
    assert "minimum requirement of 5 guests" in content["terms_text"]
    assert "For example" not in content["terms_text"]  # would otherwise say "-5 guests"


def test_lounge_has_no_fabricated_terms(db, lounge):
    booking = _booking_in(db, lounge)
    content = generate_agreement_content(booking)
    # One section, whose body is the REVIEW marker -- same shape as every
    # other space so the template and the staff editor need no special case.
    assert len(content["terms_sections"]) == 1
    assert content["terms_sections"][0]["body"].startswith("[REVIEW]")
    assert "minimum requirement" not in content["terms_text"]  # Lounge has no guest minimum at all


def test_agreement_terms_are_structured_sections_not_one_blob(db, loft):
    content = generate_agreement_content(_booking_in(db, loft))
    headings = [s["heading"] for s in content["terms_sections"]]
    assert "Deposits" in headings
    assert "Cancellation Policy" in headings
    assert all(s["body"] for s in content["terms_sections"])
    # terms_text stays the flattened equivalent, for anything wanting one block
    assert content["terms_text"] == rebuild_terms_text(content["terms_sections"])


def test_18th_conditions_are_their_own_section_without_a_duplicated_heading(db, loft):
    content = generate_agreement_content(_booking_in(db, loft, event_type="18th Birthday"))
    eighteenth = [s for s in content["terms_sections"] if s["heading"] == "18th Birthday Conditions"]
    assert len(eighteenth) == 1
    # The heading lives on the section, not repeated at the top of the body.
    assert not eighteenth[0]["body"].startswith("18th Birthday Conditions")


def test_18th_birthday_appends_the_conditions_block(db, loft):
    booking = _booking_in(db, loft, event_type="18th Birthday")
    content = generate_agreement_content(booking)
    assert "18th Birthday Conditions" in content["terms_text"]
    assert "Strict RSA compliance" in content["terms_text"]


def test_18th_detected_from_event_name_also_appends_conditions(db, loft):
    booking = _booking_in(db, loft, event_type="Birthday", event_name="Sam's 18th birthday")
    content = generate_agreement_content(booking)
    assert "18th Birthday Conditions" in content["terms_text"]


def test_non_18th_does_not_append_conditions(db, loft):
    booking = _booking_in(db, loft, event_type="Wedding")
    content = generate_agreement_content(booking)
    assert "18th Birthday Conditions" not in content["terms_text"]


def test_agreement_content_uses_correct_venue_contact_not_the_known_error(db, loft):
    """A real signed contract was found using hello@meantime.com.au --
    Master Policy v1.3 SS6.1 says that address is superseded for functions
    correspondence. This must never recur in a generated document."""
    booking = _booking_in(db, loft)
    content = generate_agreement_content(booking)
    assert content["venue_contact_email"] == "meantimehamilton@gmail.com"
    assert content["venue_contact_email"] != "hello@meantime.com.au"
    assert content["venue_contact_name"] == "Aaron"


def test_agreement_content_includes_abn_and_address(db, loft):
    booking = _booking_in(db, loft)
    content = generate_agreement_content(booking)
    assert content["venue_abn"] == "36 654 270 532"
    assert "Beaumont St" in content["venue_address"]


# --- PDF download --------------------------------------------------------------


def test_draft_document_pdf_download_404s(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}/pdf")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_sent_document_pdf_downloads_as_a_real_pdf(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"
        assert "attachment" in resp.headers["content-disposition"]
    finally:
        app.dependency_overrides.clear()


def test_pdf_download_does_not_show_the_interactive_sign_form(db, booking):
    """A downloaded PDF is static -- an unusable 'type your name' field in
    it would be actively misleading, not just unnecessary."""
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(f"/d/{document.access_token}/pdf")
        assert resp.status_code == 200
        assert b"Accept" not in resp.content or b"signer_name" not in resp.content
    finally:
        app.dependency_overrides.clear()


# --- draft-only delete ----------------------------------------------------------


def test_delete_draft_document_succeeds(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document_id = document.id
    delete_draft(db, document, actor="test")
    assert db.get(type(document), document_id) is None


def test_delete_sent_document_is_rejected(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")
    with pytest.raises(ValueError):
        delete_draft(db, document, actor="test")
    # still there, untouched
    assert get_by_token(db, document.access_token) is not None


# --- draft-only content editing --------------------------------------------------


def test_update_content_persists_an_edited_clause(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    content = dict(document.content)
    sections = [dict(s) for s in content["terms_sections"]]
    sections[0]["body"] = "A hand-negotiated deposit arrangement applies to this booking."
    content["terms_sections"] = sections
    content["terms_text"] = rebuild_terms_text(sections)

    update_content(db, document, content, actor="staff:aaron@meantime.com.au")

    assert document.content["terms_sections"][0]["body"].startswith("A hand-negotiated deposit")
    assert "A hand-negotiated deposit" in document.content["terms_text"]
    assert document.version == 1  # edited in place, not versioned per save
    assert "document_edited" in {e.event_type for e in booking.events}


def test_update_content_rejected_once_sent(db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    document = mark_sent(db, document, actor="test")
    original = dict(document.content)

    with pytest.raises(ValueError):
        update_content(db, document, {"terms_sections": [{"heading": "X", "body": "Y"}]}, actor="test")

    db.refresh(document)
    assert document.content == original


def test_edit_form_is_refused_for_a_sent_document(admin_client, db, booking):
    document = create_new_version(db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test")
    mark_sent(db, document, actor="test")
    response = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert response.status_code == 409


def test_editing_a_beo_line_item_recomputes_the_total(admin_client, db, booking):
    import re
    from decimal import Decimal

    content = generate_beo_content(
        booking,
        [{"item": "Grazing Platter", "quantity": 1, "unit_price": "250.00"}],
        deposit_paid=Decimal("500.00"),
    )
    document = create_new_version(db, booking, DocumentType.beo, content, actor="test")

    form = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert form.status_code == 200
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            "csrf_token": csrf_token,
            "item_descriptions": ["Grazing Platter"],
            "item_quantities": ["4"],
            "item_unit_prices": ["250.00"],
            "catering_order_and_service_style": "Shared platters on arrival",
            "bar_structure": "Bar tab",
            "room_layout_notes": "Long table",
            "music_entertainment": "DJ",
            "special_notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.refresh(document)
    assert document.content["food_order"]["line_items"][0]["quantity"] == 4
    assert document.content["total_food_spend"]["total"] == "1000.00"
    # deposit carried through untouched, balance recomputed against it
    assert document.content["total_food_spend"]["deposit_paid"] == "500.00"
    assert document.content["total_food_spend"]["balance_due"] == "500.00"


def test_editing_a_beo_food_order_clears_the_no_food_order_note(admin_client, db, booking):
    """A BEO generated with no food order carries a '[REVIEW] add a food
    order' note. Once one is typed in, that note must not survive -- it
    would contradict the total sitting right next to it."""
    import re

    document = create_new_version(db, booking, DocumentType.beo, generate_beo_content(booking), actor="test")
    assert "add a food order" in document.content["total_food_spend"]["note"]

    form = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={
            "csrf_token": csrf_token,
            "item_descriptions": ["Grazing Platter"],
            "item_quantities": ["3"],
            "item_unit_prices": ["250.00"],
            "catering_order_and_service_style": "Shared platters",
            "bar_structure": "Cash bar",
            "room_layout_notes": "",
            "music_entertainment": "Spotify",
            "special_notes": "",
        },
        follow_redirects=False,
    )

    db.refresh(document)
    totals = document.content["total_food_spend"]
    assert totals["total"] == "750.00"
    assert "add a food order" not in (totals["note"] or "")


# --- one-page print layout ---------------------------------------------------


def _agreement_pdf_pages(db, booking) -> int:
    import io

    # pypdf, not a rasterizing library: it ships as a dependency of
    # xhtml2pdf itself, so counting pages needs nothing new installed.
    from pypdf import PdfReader

    from app.services.pdf import render_html_to_pdf
    from app.templating import templates

    document = create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )
    return len(PdfReader(io.BytesIO(render_html_to_pdf(html))).pages)


def test_agreement_pdf_fits_one_page_worst_case(db, loft):
    """An 18th at the Loft is the longest agreement this system produces:
    all ten standard clauses plus the conditions appendix. If that fits on
    one page, everything shorter does."""
    booking = _booking_in(db, loft, event_type="18th Birthday", adult_count=60)
    content = generate_agreement_content(booking)
    assert len(content["terms_sections"]) == 11  # 10 standard + the 18th appendix
    assert _agreement_pdf_pages(db, booking) == 1


def test_agreement_pdf_fits_one_page_for_a_short_agreement(db, lounge):
    # The Lounge has a single [REVIEW] clause -- the opposite extreme.
    assert _agreement_pdf_pages(db, _booking_in(db, lounge)) == 1


def test_balance_columns_splits_on_height_not_clause_count():
    from app.templating import balance_columns

    sections = [{"heading": "Long", "body": "x" * 900}] + [
        {"heading": f"Short {i}", "body": "y" * 40} for i in range(6)
    ]
    left, right = balance_columns(sections)
    # Splitting 7 clauses down the middle by count would put the one huge
    # clause plus three others on the left. Height-balanced, it stands alone.
    assert len(left) == 1
    assert len(right) == 6
    assert left[0]["heading"] == "Long"


def test_balance_columns_handles_a_single_clause():
    from app.templating import balance_columns

    left, right = balance_columns([{"heading": "Terms", "body": "Only one."}])
    assert len(left) == 1
    assert right == []
    assert balance_columns([]) == ([], [])


def test_agreement_pdf_uses_the_wordmark_when_no_logo_file_is_present(db, loft, monkeypatch):
    """The logo is an optional deploy-time asset. Without it the document
    must still look deliberate -- never a broken-image box on a contract."""
    from app import templating

    monkeypatch.setattr(templating, "has_venue_logo", lambda: False)
    templating.templates.env.globals["has_venue_logo"] = lambda: False

    booking = _booking_in(db, loft)
    document = create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    html = templating.templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )
    assert "doc-wordmark" in html
    assert "<img" not in html
