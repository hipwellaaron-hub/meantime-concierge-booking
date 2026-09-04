import datetime as dt
import re
from decimal import Decimal

from sqlalchemy import select

from app.models import Booking, BookingEvent, Contact, Document, Invoice
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import InvoiceStatus
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import add_linked_space, change_status, create_booking, flag_for_review


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _detail_page(admin_client, booking_id):
    return admin_client.get(f"/admin/bookings/{booking_id}")


def test_bookings_list_shows_booking(admin_client, booking):
    resp = admin_client.get("/admin/bookings")
    assert resp.status_code == 200
    assert "Wilson Wedding" in resp.text
    assert booking.reference_code in resp.text


def test_booking_detail_shows_enquiry_details_and_notes(admin_client, booking):
    """Regression: the free-text comments, company name, and dates-flexible
    answer a client gave on the enquiry form were captured correctly into
    Booking.notes but never rendered anywhere on the booking detail page --
    staff had no way to see what a client actually wrote."""
    resp = _detail_page(admin_client, booking.id)
    assert resp.status_code == 200
    assert "Enquiry details" in resp.text
    assert "Bride requests no seafood." in resp.text
    assert booking.event_type in resp.text


def test_booking_detail_omits_enquiry_details_card_when_nothing_to_show(admin_client, db, unassigned_space):
    from app.services.booking import create_booking

    bare = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 5, 1),
        event_name="Bare Booking", event_type=None, adult_count=10, child_count=0,
        notes=None, actor="test",
    )
    resp = _detail_page(admin_client, bare.id)
    assert "Enquiry details" not in resp.text


def test_bookings_list_default_shows_active_booking(admin_client, booking):
    """Regression: submitting the default option posts status="", which
    Optional[BookingStatus] does not coerce to None the way Optional[str]
    does -- this used to 422 instead of showing the list. The Wilson
    Wedding fixture is an enquiry (active), so it shows by default."""
    resp = admin_client.get("/admin/bookings", params={"status": ""})
    assert resp.status_code == 200
    assert "Wilson Wedding" in resp.text


def test_bookings_list_default_hides_terminal_bookings(admin_client, db, loft):
    """The default view is the live pipeline only -- completed, cancelled,
    dead and archived bookings must not clutter it."""
    active = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 9, 1), event_name="Active Enquiry",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    archived = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 9, 2), event_name="Archived Party",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    change_status(db, archived, BookingStatus.archived, actor="test")

    default = admin_client.get("/admin/bookings")
    assert "Active Enquiry" in default.text
    assert "Archived Party" not in default.text

    # ...but reachable by picking that status explicitly,
    explicit = admin_client.get("/admin/bookings", params={"status": "archived"})
    assert "Archived Party" in explicit.text
    assert "Active Enquiry" not in explicit.text

    # ...or via the "all" view.
    all_view = admin_client.get("/admin/bookings", params={"status": "all"})
    assert "Archived Party" in all_view.text
    assert "Active Enquiry" in all_view.text


def test_bookings_list_unknown_status_returns_422_not_500(admin_client):
    resp = admin_client.get("/admin/bookings", params={"status": "not-a-real-status"})
    assert resp.status_code == 422


def test_bookings_list_sorts_soonest_event_date_first(admin_client, db, loft):
    later = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 8, 1), event_name="Later Booking",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    sooner = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2026, 9, 1), event_name="Sooner Booking",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )

    resp = admin_client.get("/admin/bookings")
    assert resp.status_code == 200
    assert resp.text.index("Sooner Booking") < resp.text.index("Later Booking")


def test_bookings_list_search_filters(admin_client, booking):
    resp = admin_client.get("/admin/bookings", params={"q": "does-not-exist"})
    assert "Wilson Wedding" not in resp.text

    resp = admin_client.get("/admin/bookings", params={"q": "Wilson"})
    assert "Wilson Wedding" in resp.text


def test_booking_detail_renders(admin_client, booking):
    resp = _detail_page(admin_client, booking.id)
    assert resp.status_code == 200
    assert "Wilson Wedding" in resp.text


# --- Linked spaces (one event, two rooms) ---------------------------------------


def test_add_linked_space_via_dashboard(admin_client, db, booking, mezzanine):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/linked-spaces",
        data={"csrf_token": csrf_token, "space_id": str(mezzanine.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(booking)
    assert len(booking.linked_bookings) == 1
    child = booking.linked_bookings[0]
    assert child.space_id == mezzanine.id
    assert child.parent_booking_id == booking.id

    parent_page = _detail_page(admin_client, booking.id)
    assert "The Mezzanine" in parent_page.text

    child_page = _detail_page(admin_client, child.id)
    assert "linked booking" in child_page.text
    assert "Wilson Wedding" in child_page.text  # links back to the parent by name


def test_linked_child_page_hides_documents_invoices_and_wizard(admin_client, db, booking, mezzanine):
    from app.services.booking import add_linked_space

    child = add_linked_space(db, booking, space_id=mezzanine.id, actor="test")
    resp = _detail_page(admin_client, child.id)
    assert resp.status_code == 200
    assert "<h2>Documents</h2>" not in resp.text
    assert "<h2>Invoices</h2>" not in resp.text
    assert "<h2>Guided Booking Wizard</h2>" not in resp.text
    assert "<h2>Policy overrides</h2>" in resp.text  # still available on a child


def test_adding_a_conflicting_linked_space_returns_409(admin_client, db, booking, mezzanine):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status, create_booking

    change_status(db, booking, BookingStatus.confirmed, actor="test")
    create_booking(
        db, space_id=mezzanine.id, contact_id=None, event_date=booking.event_date,
        start_time=booking.start_time, end_time=booking.end_time, event_name="Unrelated",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
        status=BookingStatus.confirmed,
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/linked-spaces",
        data={"csrf_token": csrf_token, "space_id": str(mezzanine.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 409


# --- Staff-created bookings (phone / direct email / iVvy marketplace) -----------


def _new_booking_payload(**overrides):
    payload = dict(
        first_name="Jordan",
        last_name="Reyes",
        email="jordan.reyes@example.com",
        phone="0411222333",
        event_name="Reyes 40th",
        event_date="2026-12-05",
        dates_flexible="false",
        event_type="Birthday",
        attendee_count="70",
        proposed_time_slot="Evening",
        comments="Called in, wants The Loft.",
        lead_source="phone",
    )
    payload.update(overrides)
    return payload


def test_new_booking_form_renders(admin_client):
    resp = admin_client.get("/admin/bookings/new")
    assert resp.status_code == 200
    assert "How did this lead reach you?" in resp.text


def test_staff_create_booking_via_phone(admin_client, db, unassigned_space):
    page = admin_client.get("/admin/bookings/new")
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        "/admin/bookings/new", data={**_new_booking_payload(), "csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/bookings/")

    booking = db.query(Booking).filter_by(event_name="Reyes 40th").one()
    assert booking.status == BookingStatus.enquiry
    assert booking.space_id == unassigned_space.id
    assert booking.lead_source == "phone"
    assert booking.contact.email == "jordan.reyes@example.com"
    # Went through the same classification pipeline as the public form --
    # a generic "Birthday" enquiry gets flagged the same way regardless of
    # who entered it.
    events = [e.new_value for e in booking.events if e.event_type == "enquiry_flagged"]
    assert any("Generic 'Birthday'" in e for e in events)


def test_staff_created_booking_records_unknown_attribution(admin_client, db, unassigned_space):
    """A phone call has no page load to capture UTM/referrer data from --
    both columns must stay NULL (genuinely "never tracked"), never
    silently defaulted into "direct" or "organic"."""
    page = admin_client.get("/admin/bookings/new")
    csrf_token = _csrf(page.text)

    admin_client.post(
        "/admin/bookings/new", data={**_new_booking_payload(), "csrf_token": csrf_token}, follow_redirects=False
    )

    booking = db.query(Booking).filter_by(event_name="Reyes 40th").one()
    assert booking.first_touch_attribution is None
    assert booking.last_touch_attribution is None

    detail = admin_client.get(f"/admin/bookings/{booking.id}")
    assert "Unknown -- not tracked" in detail.text


def test_staff_create_booking_requires_a_lead_source(admin_client):
    page = admin_client.get("/admin/bookings/new")
    csrf_token = _csrf(page.text)

    payload = _new_booking_payload()
    del payload["lead_source"]
    resp = admin_client.post("/admin/bookings/new", data={**payload, "csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 422


def test_staff_create_booking_reuses_recent_duplicate_not_a_second_booking(admin_client, db):
    page = admin_client.get("/admin/bookings/new")
    csrf_token = _csrf(page.text)
    payload = {**_new_booking_payload(), "csrf_token": csrf_token}

    resp1 = admin_client.post("/admin/bookings/new", data=payload, follow_redirects=False)
    resp2 = admin_client.post("/admin/bookings/new", data=payload, follow_redirects=False)

    assert resp1.headers["location"] == resp2.headers["location"]
    assert db.query(Booking).filter_by(event_name="Reyes 40th").count() == 1


def test_draft_document_has_no_dead_view_link(admin_client, db, booking):
    """The public /d/{token} route 404s on a draft document by design (not
    yet human-approved for client eyes) -- the detail page must not offer a
    View link that leads straight into that 404. It should instead offer
    the staff-only Preview link, which works on a draft."""
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(booking)
    page2 = _detail_page(admin_client, booking.id)
    assert "View" not in page2.text
    assert "Preview" in page2.text
    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)
    assert f"/admin/bookings/{booking.id}/documents/{agreement.id}/preview" in page2.text


def test_staff_can_preview_a_draft_document(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    db.refresh(booking)
    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)

    resp = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/preview")
    assert resp.status_code == 200
    assert "Staff preview" in resp.text
    assert booking.event_name in resp.text

    # Staff looking at the preview must never be mistaken for the client
    # having seen it -- status stays exactly as it was.
    db.refresh(agreement)
    assert agreement.status == DocumentStatus.draft


def test_document_preview_404s_for_a_document_on_another_booking(admin_client, db, booking, loft):
    from app.services.booking import create_booking as _create_booking
    from app.services.document_generation import generate_agreement_content

    other_booking = _create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="Other Booking",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    other_doc = documents_service.create_new_version(
        db, other_booking, DocumentType.agreement, generate_agreement_content(other_booking), actor="test"
    )
    resp = admin_client.get(f"/admin/bookings/{booking.id}/documents/{other_doc.id}/preview")
    assert resp.status_code == 404


def test_sent_document_has_a_working_view_link(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    db.refresh(booking)

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/send",
        data={"csrf_token": csrf_token2}, follow_redirects=False,
    )
    db.refresh(booking)

    page3 = _detail_page(admin_client, booking.id)
    assert "View" in page3.text
    view_resp = admin_client.get(f"/d/{agreement.access_token}")
    assert view_resp.status_code == 200


def test_generate_and_send_beo(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert beo is not None
    assert beo.status == DocumentStatus.draft

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{beo.id}/send",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(beo)
    assert beo.status == DocumentStatus.sent


def test_send_document_refused_and_flagged_when_booking_has_no_contact(admin_client, db, loft):
    contactless_booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="No Contact Booking",
        event_type="party", adult_count=10, child_count=0, notes=None, actor="test",
    )
    page = _detail_page(admin_client, contactless_booking.id)
    assert "no contact on file" in page.text  # the visible flag
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{contactless_booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    db.refresh(contactless_booking)

    page2 = _detail_page(admin_client, contactless_booking.id)
    csrf_token2 = _csrf(page2.text)
    agreement = documents_service.get_current(db, contactless_booking.id, DocumentType.agreement)
    resp = admin_client.post(
        f"/admin/bookings/{contactless_booking.id}/documents/{agreement.id}/send",
        data={"csrf_token": csrf_token2}, follow_redirects=False,
    )
    assert resp.status_code == 409
    db.refresh(agreement)
    assert agreement.status == DocumentStatus.draft


def test_staff_can_preview_a_draft_invoice(admin_client, db, booking):
    from app.services.invoicing import create_deposit_invoice

    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 9, 1), actor="test")

    resp = admin_client.get(f"/admin/bookings/{booking.id}/invoices/{invoice.id}/preview")
    assert resp.status_code == 200
    assert "Staff preview" in resp.text
    assert str(invoice.invoice_number) in resp.text

    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.draft


def test_invoice_preview_404s_for_an_invoice_on_another_booking(admin_client, db, booking, loft):
    from app.services.booking import create_booking as _create_booking
    from app.services.invoicing import create_deposit_invoice

    other_booking = _create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 6, 1),
        start_time=dt.time(12, 0), end_time=dt.time(17, 0), event_name="Other Booking",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    other_invoice = create_deposit_invoice(db, other_booking, due_date=dt.date(2026, 9, 1), actor="test")
    resp = admin_client.get(f"/admin/bookings/{booking.id}/invoices/{other_invoice.id}/preview")
    assert resp.status_code == 404


def test_create_send_and_pay_deposit_invoice(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/deposit",
        data={"csrf_token": csrf_token, "due_date": "2026-09-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    invoice = booking.invoices[0]
    assert invoice.status == InvoiceStatus.draft

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/send",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.sent

    page3 = _detail_page(admin_client, booking.id)
    csrf_token3 = _csrf(page3.text)
    resp3 = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/payments",
        data={
            "csrf_token": csrf_token3,
            "amount": str(invoice.total),
            "method": "bank_transfer",
            "reference": "TXN123",
            "payer_name": "Pat Wilson",
        },
        follow_redirects=False,
    )
    assert resp3.status_code == 303
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_invoices_card_shows_stripe_test_mode_badge(admin_client, booking):
    from unittest.mock import patch

    from app.services import stripe_integration

    with patch.object(stripe_integration, "STRIPE_SECRET_KEY", "sk_test_fake"):
        resp = _detail_page(admin_client, booking.id)

    invoices_section = resp.text.split('id="sec-invoices"')[1].split("</h2>")[0]
    assert "Stripe test mode -- no real charges" in invoices_section


def test_create_final_invoice_without_wizard(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/final",
        data={
            "csrf_token": csrf_token,
            "due_date": "2026-10-01",
            "description": ["Catering", "", "", "", "", ""],
            "quantity": ["1", "1", "1", "1", "1", "1"],
            "unit_price": ["350.00", "", "", "", "", ""],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    invoice = [i for i in booking.invoices if i.type.value == "final"][0]
    assert invoice.status == InvoiceStatus.draft
    assert invoice.total == Decimal("350.00")


def test_create_final_invoice_rejects_duplicate_via_dashboard(admin_client, db, booking):
    invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Catering", "quantity": 1, "unit_price": "350.00"}],
        due_date=dt.date(2026, 10, 1), actor="test",
    )
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/final",
        data={
            "csrf_token": csrf_token,
            "due_date": "2026-10-01",
            "description": ["Catering", "", "", "", "", ""],
            "quantity": ["1", "1", "1", "1", "1", "1"],
            "unit_price": ["350.00", "", "", "", "", ""],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_create_final_invoice_rejects_all_blank_line_items(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/final",
        data={
            "csrf_token": csrf_token,
            "due_date": "2026-10-01",
            "description": ["", "", "", "", "", ""],
            "quantity": ["1", "1", "1", "1", "1", "1"],
            "unit_price": ["", "", "", "", "", ""],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_send_and_revoke_wizard_link(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session is not None
    token = booking.wizard_session.access_token
    assert token in _detail_page(admin_client, booking.id).text

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/revoke", data={"csrf_token": csrf_token2}, follow_redirects=False
    )
    assert resp2.status_code == 303
    db.refresh(booking)
    assert booking.wizard_session.status.value == "revoked"


def test_confirm_early_setup_access(admin_client, db, booking):
    booking.setup_access_time = dt.time(11, 0)
    booking.setup_access_confirmed = False
    db.commit()

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.setup_access_confirmed is True


def test_confirm_early_setup_access_with_no_pending_request_errors(admin_client, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/setup-access/confirm",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_set_agreed_minimum_requires_reason_when_reduced(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={"csrf_token": csrf_token, "agreed_min_adults": str(booking.space.standard_min_adults - 5)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.agreed_min_adults == booking.space.standard_min_adults


def test_set_agreed_minimum_with_reason_succeeds(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    reduced = booking.space.standard_min_adults - 5
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/agreed-minimum",
        data={
            "csrf_token": csrf_token,
            "agreed_min_adults": str(reduced),
            "reason": MinReductionReasonCode.returning_client.value,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == reduced
    assert booking.agreed_min_reduction_reason == MinReductionReasonCode.returning_client


def test_toggle_outside_cake_permitted(admin_client, db, booking):
    assert booking.outside_cake_permitted is False

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake",
        data={"csrf_token": csrf_token, "permitted": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.outside_cake_permitted is True

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp2 = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake",
        data={"csrf_token": csrf_token2, "permitted": "false"},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    db.refresh(booking)
    assert booking.outside_cake_permitted is False


def test_booking_detail_shows_no_contact_banner_when_missing(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2026, 11, 7),
        event_name="Imported No Contact", event_type=None, adult_count=30, child_count=0,
        notes=None, actor="test",
    )
    resp = _detail_page(admin_client, booking.id)
    assert "No contact on file" in resp.text


def test_add_contact_to_a_booking_with_none(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2026, 11, 7),
        event_name="Imported No Contact", event_type=None, adult_count=30, child_count=0,
        notes=None, actor="test",
    )
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/contact",
        data={"csrf_token": csrf_token, "name": "Melanie Sterjovski", "email": "melaniesterjovskihair@hotmail.com", "phone": "0400000000"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(booking)
    assert booking.contact is not None
    assert booking.contact.email == "melaniesterjovskihair@hotmail.com"
    assert booking.contact.name == "Melanie Sterjovski"

    after = _detail_page(admin_client, booking.id)
    assert "No contact on file" not in after.text
    assert "melaniesterjovskihair@hotmail.com" in after.text


def test_change_contact_reuses_an_existing_contact_by_email(admin_client, db, unassigned_space, booking):
    """An email that already matches an existing Contact is reused, not
    duplicated -- same dedupe rule as everywhere else a contact is
    resolved from an email."""
    from app.models import Contact

    other_booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2026, 11, 7),
        event_name="Second Booking Same Client", event_type=None, adult_count=10, child_count=0,
        notes=None, actor="test",
    )
    page = _detail_page(admin_client, other_booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{other_booking.id}/contact",
        data={"csrf_token": csrf_token, "name": "Wilson Wedding Contact", "email": booking.contact.email, "phone": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.refresh(other_booking)
    assert other_booking.contact_id == booking.contact_id
    assert db.query(Contact).filter_by(email=booking.contact.email).count() == 1


def test_add_contact_requires_name_and_email(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2026, 11, 7),
        event_name="Imported No Contact", event_type=None, adult_count=30, child_count=0,
        notes=None, actor="test",
    )
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/contact",
        data={"csrf_token": csrf_token, "name": "", "email": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_flag_for_review_surfaces_on_the_clarification_banner(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2026, 11, 28),
        event_name="Alyssa Booking", event_type=None, adult_count=50, child_count=0,
        notes=None, actor="test",
    )
    flag_for_review(
        db, booking, note="iVvy recorded 'Alyssa Boswood'; the live email thread says 'Alyssa Ross' -- confirm same person.",
        actor="test",
    )

    resp = _detail_page(admin_client, booking.id)
    assert "Needs clarification before proceeding" in resp.text
    assert "Alyssa Boswood" in resp.text
    assert "Alyssa Ross" in resp.text


def test_assign_space_conflict_returns_409(admin_client, db, loft, unassigned_space):
    existing = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 8, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Existing Confirmed",
        event_type=None, adult_count=10, child_count=0, notes=None, actor="test",
    )
    change_status(db, existing, BookingStatus.confirmed, actor="test")

    conflicting = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=dt.date(2027, 8, 1),
        event_name="Needs Triage", event_type=None, adult_count=10, child_count=0,
        notes=None, actor="test", status=BookingStatus.confirmed,
    )

    page = _detail_page(admin_client, conflicting.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{conflicting.id}/assign-space",
        data={"csrf_token": csrf_token, "space_id": str(loft.id), "start_time": "18:00", "end_time": "23:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_assign_space_can_set_a_previously_missing_event_date(admin_client, db, loft, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="No Date Yet", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.event_date == dt.date(2027, 9, 4)


def test_assign_space_with_blank_event_date_leaves_it_unset(admin_client, db, loft, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="Still No Date", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={"csrf_token": csrf_token, "space_id": str(loft.id), "start_time": "18:00", "end_time": "23:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.event_date is None


def test_assign_space_reconciles_untouched_agreed_minimum_to_new_space_standard(
    admin_client, db, loft, unassigned_space
):
    """A booking created in the Unassigned placeholder (standard_min_adults
    always 0) must pick up the real space's standard minimum on assignment,
    not carry the stale 0 forward with no reason recorded."""
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="From Unassigned", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )
    assert booking.agreed_min_adults == unassigned_space.standard_min_adults

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == loft.standard_min_adults
    assert booking.agreed_min_reduction_reason is None


def test_assign_space_leaves_a_deliberately_set_agreed_minimum_alone(admin_client, db, loft, unassigned_space):
    from app.models.booking import MinReductionReasonCode
    from app.services.booking import set_agreed_minimum

    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="Custom Minimum Already Set", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )
    set_agreed_minimum(
        db, booking, agreed_min_adults=5, reason=MinReductionReasonCode.aaron_discretion, actor="test"
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/assign-space",
        data={
            "csrf_token": csrf_token, "space_id": str(loft.id),
            "start_time": "18:00", "end_time": "23:00", "event_date": "2027-09-04",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.agreed_min_adults == 5
    assert booking.agreed_min_reduction_reason == MinReductionReasonCode.aaron_discretion


def test_transition_status_via_dashboard_succeeds(admin_client, db, booking):
    assert booking.status == BookingStatus.enquiry
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/status",
        data={"csrf_token": csrf_token, "new_status": "offered", "reason": "quoted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(booking)
    assert booking.status == BookingStatus.offered


def test_transition_status_via_dashboard_rejects_illegal_move(admin_client, db, booking):
    assert booking.status == BookingStatus.enquiry
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/status",
        data={"csrf_token": csrf_token, "new_status": "confirmed"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.status == BookingStatus.enquiry


def test_booking_detail_shows_only_legal_next_statuses(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    assert 'value="offered"' in page.text
    assert 'value="dead"' in page.text
    assert 'value="confirmed"' not in page.text
    assert 'value="completed"' not in page.text


def test_sending_wizard_link_without_event_date_is_rejected(admin_client, db, unassigned_space):
    booking = create_booking(
        db, space_id=unassigned_space.id, contact_id=None, event_date=None,
        event_name="No Date Yet", event_type="Wedding", adult_count=50, child_count=0,
        notes=None, actor="test",
    )

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/wizard/send", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.wizard_session is None


def test_delete_draft_agreement_via_dashboard(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    db.refresh(booking)
    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/delete",
        data={"csrf_token": csrf_token2}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.get(type(agreement), agreement.id) is None


def test_delete_sent_agreement_via_dashboard_is_rejected(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    db.refresh(booking)
    agreement = documents_service.get_current(db, booking.id, DocumentType.agreement)

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/send",
        data={"csrf_token": csrf_token2}, follow_redirects=False,
    )
    db.refresh(booking)

    page3 = _detail_page(admin_client, booking.id)
    csrf_token3 = _csrf(page3.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/delete",
        data={"csrf_token": csrf_token3}, follow_redirects=False,
    )
    assert resp.status_code == 409
    assert db.get(type(agreement), agreement.id) is not None


def test_delete_draft_invoice_via_dashboard(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/deposit",
        data={"csrf_token": csrf_token, "due_date": "2026-09-01"}, follow_redirects=False,
    )
    db.refresh(booking)
    invoice = booking.invoices[0]

    page2 = _detail_page(admin_client, booking.id)
    csrf_token2 = _csrf(page2.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/delete",
        data={"csrf_token": csrf_token2}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.get(type(invoice), invoice.id) is None


def test_dashboard_beo_regenerate_uses_real_wizard_data_when_submitted(admin_client, db, loft, menu_items):
    from app.services import wizard as wizard_service
    from app.services.booking import create_booking
    from app.services.wizard import BarStructure, CakeChoiceType, MusicType

    contact = Contact(name="Wizard BEO Test Contact", email="wizard-beo-test@example.com")
    db.add(contact)
    db.flush()
    wizard_booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 6, 12),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Wizard BEO Test",
        event_type="birthday", adult_count=60, child_count=0, notes=None, actor="test",
    )
    session = wizard_service.get_or_create_session(db, wizard_booking, actor="test")
    grazing = menu_items["Grazing Platter"]
    wizard_service.save_basics_step(
        db, session, start_time=wizard_booking.start_time, end_time=wizard_booking.end_time,
        food_service_time=dt.time(18, 30), setup_access_time=dt.time(14, 0),
        adult_count=60, child_count=0, actor="test",
    )
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 2}], pizzas=[], actor="test"
    )
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="test"
    )
    wizard_service.save_music_step(
        db, session, music_type=MusicType.own_playlist, notes="Chill playlist", bump_in_notes=None, actor="test"
    )
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes="No special layout", dietary_requirements=None,
        accessibility_needs=None, additional_notes=None, actor="test",
    )
    wizard_service.submit_review(db, session, actor="test")
    db.refresh(wizard_booking)

    # Now staff manually hits Regenerate from the dashboard -- must reuse
    # the real wizard answers, not overwrite them with blank [REVIEW]
    # placeholders.
    page = _detail_page(admin_client, wizard_booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{wizard_booking.id}/documents/beo/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    assert resp.status_code == 303

    beo = documents_service.get_current(db, wizard_booking.id, DocumentType.beo)
    assert "Grazing Platter" in [li["description"] for li in beo.content["food_order"]["line_items"]]
    assert "Cash bar" in beo.content["bar_structure"]
    # Music and entertainment are separate sections now -- the playlist
    # note lands in the dedicated music field.
    assert "Chill playlist" in beo.content["music"]
    timeline_bullets = " ".join(beo.content["event_timeline"]["bullets"])
    assert "Setup access from 2:00pm" in timeline_bullets
    assert "Food service from 6:30pm" in timeline_bullets
    assert "[REVIEW]" not in beo.content["catering_order_and_service_style"]


def test_dashboard_beo_generate_still_blank_when_no_wizard_session(admin_client, db, booking):
    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": csrf_token}, follow_redirects=False,
    )
    assert resp.status_code == 303

    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert "[REVIEW]" in beo.content["bar_structure"]
    assert "[REVIEW]" in beo.content["event_timeline"]["notes"]


# --- enquiry notification failure + resend -----------------------------------


def _create_enquiry(db, *, email, event_name, event_date):
    from app.models import Venue
    from app.services.enquiry_classification import create_enquiry_booking

    venue = db.query(Venue).filter_by(slug="hamilton").one()
    booking, _duplicate_candidates, _is_new = create_enquiry_booking(
        db, venue=venue, full_name="Notify Test", email=email, phone=None,
        event_name=event_name, event_type="Wedding", event_date=event_date,
        proposed_time_slot=None, attendee_count=50, adult_count=50, company_name=None,
        dates_flexible=False, comments=None, lead_source="direct", lead_referrer=None, actor="test",
    )
    return booking


def test_booking_detail_shows_enquiry_notification_failed_banner_with_resend(admin_client, db, unassigned_space):
    # Gmail SMTP isn't configured in tests, so create_enquiry_booking's
    # own call to notify_new_enquiry records a genuine, unmocked failure --
    # exactly the state this banner exists to surface.
    booking = _create_enquiry(db, email="notify.detail@example.com", event_name="Notify Detail Booking", event_date=dt.date(2027, 5, 1))
    assert booking.enquiry_notification_sent_at is None

    resp = _detail_page(admin_client, booking.id)
    assert "Enquiry notification failed to send" in resp.text
    assert "Resend notification" in resp.text


def test_resend_enquiry_notification_route_clears_the_failure(admin_client, db, unassigned_space):
    from unittest.mock import patch

    booking = _create_enquiry(db, email="resend.route@example.com", event_name="Resend Route Booking", event_date=dt.date(2027, 5, 2))
    assert booking.enquiry_notification_sent_at is None

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    with patch("app.services.notifications.send_enquiry_notification_email"):
        resp = admin_client.post(
            f"/admin/bookings/{booking.id}/enquiry-notification/resend",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is not None

    after = _detail_page(admin_client, booking.id)
    assert "Enquiry notification failed to send" not in after.text


def test_resend_enquiry_notification_route_surfaces_failure(admin_client, db, unassigned_space):
    from unittest.mock import patch

    booking = _create_enquiry(db, email="resend.fail@example.com", event_name="Resend Fail Booking", event_date=dt.date(2027, 5, 3))

    page = _detail_page(admin_client, booking.id)
    csrf_token = _csrf(page.text)

    with patch("app.services.notifications.send_enquiry_notification_email", side_effect=RuntimeError("still down")):
        resp = admin_client.post(
            f"/admin/bookings/{booking.id}/enquiry-notification/resend",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
    assert resp.status_code == 502


def test_enquiry_notification_preview_shows_the_real_email(admin_client, db, loft):
    """The preview must be built by the same functions that send, so it
    can't drift from what actually goes out."""
    from app.services.enquiry_classification import preview_enquiry_notification
    from app.services.notifications import (
        ENQUIRY_NOTIFICATION_RECIPIENT,
        build_enquiry_notification_subject,
    )

    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2026, 11, 28),
        event_name="meantime Christmas party", event_type=None,
        adult_count=0, child_count=0, notes=None, actor="test",
    )

    recipient, subject, body, booking_url = preview_enquiry_notification(booking)
    assert recipient == ENQUIRY_NOTIFICATION_RECIPIENT
    assert subject == build_enquiry_notification_subject(booking)
    assert "meantime Christmas party" in subject
    assert "Reference:" in body and str(booking.reference_code) in body
    assert str(booking.id) in booking_url
    assert "/admin/bookings/" not in body  # the link is the header, never the body

    page = admin_client.get(f"/admin/bookings/{booking.id}/enquiry-notification/preview")
    assert page.status_code == 200
    assert ENQUIRY_NOTIFICATION_RECIPIENT in page.text
    assert "meantime Christmas party" in page.text
    assert booking_url in page.text


def test_enquiry_notification_preview_sends_nothing(admin_client, db, loft):
    """Opening the preview must not send, and must not record a send."""
    from unittest.mock import patch

    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2026, 11, 28),
        event_name="Preview Only", event_type=None,
        adult_count=10, child_count=0, notes=None, actor="test",
    )
    with patch("app.services.notifications._send_via_gmail_smtp") as send:
        admin_client.get(f"/admin/bookings/{booking.id}/enquiry-notification/preview")
    send.assert_not_called()
    db.refresh(booking)
    assert booking.enquiry_notification_sent_at is None


# --- invoice editing / revising (routes) --------------------------------------


def _final_invoice(db, booking):
    return invoicing.create_final_invoice(
        db,
        booking,
        line_items=[{"description": "Catering", "quantity": 1, "unit_price": "800.00"}],
        due_date=dt.date(2026, 10, 1),
        actor="test",
    )


def test_edit_invoice_form_loads_for_draft(admin_client, booking, db):
    invoice = _final_invoice(db, booking)
    page = admin_client.get(f"/admin/bookings/{booking.id}/invoices/{invoice.id}/edit")
    assert page.status_code == 200
    assert "Edit invoice" in page.text


def test_edit_invoice_form_redirects_for_sent(admin_client, booking, db):
    invoice = _final_invoice(db, booking)
    invoicing.mark_sent(db, invoice, actor="test")
    resp = admin_client.get(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/edit", follow_redirects=False
    )
    assert resp.status_code == 303


def test_edit_invoice_applies_discount(admin_client, booking, db):
    invoice = _final_invoice(db, booking)
    csrf = _csrf(admin_client.get(f"/admin/bookings/{booking.id}/invoices/{invoice.id}/edit").text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/edit",
        data={
            "csrf_token": csrf,
            "due_date": "2026-10-05",
            "description": ["Catering", "Discount"],
            "quantity": ["1", "1"],
            "unit_price": ["800.00", "-150.00"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(invoice)
    assert invoice.subtotal == Decimal("650.00")


def test_revise_sent_invoice_route_lands_on_new_draft_editor(admin_client, booking, db):
    invoice = _final_invoice(db, booking)
    invoicing.mark_sent(db, invoice, actor="test")
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/revise",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/edit" in resp.headers["location"]
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.cancelled


def test_revise_paid_invoice_is_refused(admin_client, booking, db):
    from app.models.payment import PaymentMethod

    invoice = _final_invoice(db, booking)
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=Decimal("100.00"), method=PaymentMethod.bank_transfer, actor="test")
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{invoice.id}/revise",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 409


# --- hard delete booking ------------------------------------------------------


def _booking_with_everything(db, loft, mezzanine):
    """A booking carrying an invoice + payment + document + a linked child,
    to prove the delete cascades through all of it."""
    from app.models import Contact
    from app.models.payment import PaymentMethod

    contact = Contact(name="Delete Test", email="delete.test@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 5, 1),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Delete Me",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    add_linked_space(db, booking, space_id=mezzanine.id, actor="test")
    inv = invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Catering", "quantity": 1, "unit_price": "800.00"}],
        due_date=dt.date(2027, 4, 1), actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")
    invoicing.record_payment(db, inv, amount=Decimal("100.00"), method=PaymentMethod.card, actor="test")
    documents_service.create_new_version(db, booking, DocumentType.beo, {"n": 1}, actor="test")
    return booking


def test_delete_booking_requires_exact_reference(admin_client, booking, db):
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/delete",
        data={"csrf_token": csrf, "confirm_reference": "WRONG-CODE"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert db.get(Booking, booking.id) is not None  # still there


def test_delete_booking_removes_everything(admin_client, db, loft, mezzanine):
    from app.models import Payment
    from app.models.wizard_session import WizardSession

    booking = _booking_with_everything(db, loft, mezzanine)
    booking_id = booking.id
    child_id = booking.linked_bookings[0].id
    invoice_ids = [i.id for i in booking.invoices]

    csrf = _csrf(_detail_page(admin_client, booking_id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking_id}/delete",
        data={"csrf_token": csrf, "confirm_reference": booking.reference_code},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/bookings"

    assert db.get(Booking, booking_id) is None
    assert db.get(Booking, child_id) is None  # linked child gone too
    for inv_id in invoice_ids:
        assert db.get(Invoice, inv_id) is None
    assert db.scalars(select(Payment.id).where(Payment.invoice_id.in_(invoice_ids))).first() is None
    assert db.scalars(select(Document.id).where(Document.booking_id == booking_id)).first() is None
    assert db.scalars(select(BookingEvent.id).where(BookingEvent.booking_id == booking_id)).first() is None
    assert db.scalars(select(WizardSession.id).where(WizardSession.booking_id == booking_id)).first() is None


def test_cannot_delete_a_linked_child_directly(admin_client, db, loft, mezzanine):
    booking = _booking_with_everything(db, loft, mezzanine)
    child = booking.linked_bookings[0]
    csrf = _csrf(_detail_page(admin_client, child.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{child.id}/delete",
        data={"csrf_token": csrf, "confirm_reference": child.reference_code},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert db.get(Booking, child.id) is not None


# --- recording a payment with a chosen date -----------------------------------


def _sent_deposit(db, booking):
    inv = invoicing.create_invoice(
        db, booking, __import__("app.models.invoice", fromlist=["InvoiceType"]).InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}], dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, inv, actor="test")
    return inv


def test_record_payment_with_a_back_dated_date(admin_client, booking, db):
    from app.models import Payment

    inv = _sent_deposit(db, booking)
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{inv.id}/payments",
        data={"csrf_token": csrf, "amount": "500.00", "method": "bank_transfer", "received_date": "2026-08-20"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    assert payment.received_at.date() == dt.date(2026, 8, 20)


def test_record_payment_blank_date_defaults_to_now(admin_client, booking, db):
    from app.models import Payment

    inv = _sent_deposit(db, booking)
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{inv.id}/payments",
        data={"csrf_token": csrf, "amount": "500.00", "method": "bank_transfer", "received_date": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    # Compare the stored instant in UTC (the code defaults a blank date to
    # datetime.now(utc)). received_at is TIMESTAMPTZ, so psycopg hands it
    # back in the DB session's timezone -- Australia/Sydney on a local dev
    # box, UTC on CI -- and a naive .date() would then read the local
    # calendar day, which drifts a day ahead of UTC for the 10h the two
    # zones straddle midnight. Normalising to UTC first makes the assertion
    # about the actual instant, deterministic on any session timezone.
    assert payment.received_at.astimezone(dt.timezone.utc).date() == dt.datetime.now(dt.timezone.utc).date()


def test_record_payment_rejects_a_bad_date(admin_client, booking, db):
    inv = _sent_deposit(db, booking)
    csrf = _csrf(_detail_page(admin_client, booking.id).text)
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/invoices/{inv.id}/payments",
        data={"csrf_token": csrf, "amount": "500.00", "method": "bank_transfer", "received_date": "not-a-date"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
