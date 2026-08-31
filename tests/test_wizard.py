import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Contact, MenuItem
from app.models.booking import BookingStatus, MinReductionReasonCode
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.menu_item import MenuItemCategory
from app.models.payment import PaymentMethod
from app.models.wizard_session import WizardSession, WizardSessionStatus, WizardStep
from app.services import documents as documents_service
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking
from app.services.wizard import BarStructure, CakeChoiceType, MusicType


def _make_booking(db, space, *, event_date=dt.date(2027, 3, 6), adult_count=50):
    contact = Contact(name="Wizard Test Contact", email="wizard.test@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db,
        space_id=space.id,
        contact_id=contact.id,
        event_date=event_date,
        start_time=dt.time(18, 0),
        end_time=dt.time(23, 0),
        event_name="Wizard Test Booking",
        event_type="birthday",
        adult_count=adult_count,
        child_count=0,
        notes=None,
        actor="test",
    )


def _make_eligible_booking(db, space, **kwargs):
    """Deposit paid + agreement signed -- matches the BEO gating checklist
    the wizard's eligibility query enforces."""
    booking = _make_booking(db, space, **kwargs)
    change_status(db, booking, BookingStatus.confirmed, actor="test")

    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=500, method=PaymentMethod.card, actor="test")

    agreement = documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    agreement = documents_service.mark_sent(db, agreement, actor="test")
    documents_service.sign(db, agreement, signer_name="Test Client", signer_ip="127.0.0.1")

    return booking


# --- enum persistence ------------------------------------------------------


def test_menu_item_category_persists_every_value(db):
    for i, category in enumerate(MenuItemCategory):
        item = MenuItem(category=category, name=f"Persistence Test {category.value} {i}", current_price=1, is_active=True)
        db.add(item)
    db.commit()
    for category in MenuItemCategory:
        db.expire_all()
        found = db.query(MenuItem).filter(MenuItem.category == category).first()
        assert found is not None
        assert found.category == category


def test_min_reduction_reason_code_persists_every_value(db, loft):
    for reason in MinReductionReasonCode:
        booking = _make_booking(db, loft, event_date=dt.date(2027, 4, 1))
        booking.agreed_min_reduction_reason = reason
        db.commit()
        db.refresh(booking)
        assert booking.agreed_min_reduction_reason == reason


def test_wizard_session_status_persists_every_value(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    for status in WizardSessionStatus:
        session.status = status
        db.commit()
        db.refresh(session)
        assert session.status == status


def test_wizard_step_persists_every_value(db, loft):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 4, 2))
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    for step in WizardStep:
        session.current_step = step
        db.commit()
        db.refresh(session)
        assert session.current_step == step


# --- agreed_min_adults source of truth --------------------------------------


def test_create_booking_defaults_agreed_min_adults_from_space(db, loft):
    booking = _make_booking(db, loft)
    assert booking.agreed_min_adults == loft.standard_min_adults == 60


def test_agreed_min_adults_can_be_reduced_independent_of_space_default(db, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 5, 1),
        event_name="Reduced Minimum Test", event_type=None, adult_count=50, child_count=0,
        notes=None, actor="test", agreed_min_adults=50,
    )
    db.refresh(loft)
    assert booking.agreed_min_adults == 50
    # The space's own standard is untouched -- this is the whole point of
    # Master Policy v1.3 §4: reductions are a booking-level fact, never a
    # mutation of the space's standard minimum.
    assert loft.standard_min_adults == 60


# --- session lifecycle / resumability ---------------------------------------


def test_get_or_create_session_is_idempotent(db, loft):
    booking = _make_booking(db, loft)
    first = wizard_service.get_or_create_session(db, booking, actor="test")
    second = wizard_service.get_or_create_session(db, booking, actor="test")
    assert first.id == second.id


def test_cannot_send_wizard_link_when_booking_has_no_contact(db, loft):
    booking = create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2027, 3, 6),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="No Contact Wizard Booking",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    with pytest.raises(ValueError, match="valid email"):
        wizard_service.get_or_create_session(db, booking, actor="test")


def test_food_step_saves_and_resumes(db, loft, menu_items):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]

    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 2}], pizzas=[], actor="test"
    )

    # A fresh load (simulating the client returning later) sees the saved
    # partial progress and resumes at the next step, never losing data.
    reloaded = wizard_service.get_by_token(db, session.access_token)
    assert reloaded.food_response["platters"] == [{"menu_item_id": str(grazing.id), "quantity": 2}]
    assert reloaded.current_step == WizardStep.beverage


def test_food_step_returns_guidance_matching_computed_subtotal(db, loft, menu_items):
    booking = _make_booking(db, loft, adult_count=60)  # min food spend on Loft is $1,000
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]  # $250

    guidance = wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 5}], pizzas=[], actor="test"
    )
    assert guidance.subtotal == 250 * 5
    assert guidance.met_minimum_spend is True
    assert session.food_response["guidance_subtotal"] == str(guidance.subtotal)
    assert session.food_response["needs_price_review"] == []


def test_food_step_flags_undefined_legacy_price_for_review(db, loft, menu_items):
    before_cutover = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    booking = _make_booking(db, loft)
    booking.created_at = before_cutover
    booking.pricing_locked_at = before_cutover.date()
    db.commit()
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    vegetarian = menu_items["Vegetarian Pizza"]  # no legacy price defined

    guidance = wizard_service.save_food_step(
        db, session, platters=[], pizzas=[{"menu_item_id": vegetarian.id, "quantity": 2}], actor="test"
    )
    # Excluded from the guidance subtotal entirely -- never guessed.
    assert guidance.subtotal == 0
    assert "Vegetarian Pizza" in session.food_response["needs_price_review"]
    event_types = {e.event_type for e in booking.events}
    assert "wizard_needs_review" in event_types


def test_reposting_earlier_step_does_not_regress_current_step(db, loft, menu_items):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]

    wizard_service.save_food_step(db, session, platters=[], pizzas=[], actor="test")
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="test"
    )
    assert session.current_step == WizardStep.music

    # Editing Food again (already-completed step) must not send the
    # client backward from Music to Beverage.
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 1}], pizzas=[], actor="test"
    )
    assert session.current_step == WizardStep.music


def test_food_step_rejects_unknown_menu_item(db, loft, menu_items):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    try:
        wizard_service.save_food_step(
            db, session, platters=[{"menu_item_id": uuid.uuid4(), "quantity": 1}], pizzas=[], actor="test"
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_food_step_rejects_pizza_submitted_as_platter(db, loft, menu_items):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    margherita = menu_items["Margherita Pizza"]
    try:
        wizard_service.save_food_step(
            db, session, platters=[{"menu_item_id": margherita.id, "quantity": 1}], pizzas=[], actor="test"
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_beverage_step_requires_limit_for_bar_tab(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    try:
        wizard_service.save_beverage_step(
            db, session, bar_structure=BarStructure.bar_tab, bar_limit=None, bar_inclusions=None, actor="test"
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_beverage_step_requires_limit_for_hybrid(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    try:
        wizard_service.save_beverage_step(
            db, session, bar_structure=BarStructure.hybrid, bar_limit=None, bar_inclusions=None, actor="test"
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_cash_bar_never_requires_a_limit(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="test"
    )
    assert session.beverage_response["bar_structure"] == "cash_bar"


def test_submitted_session_cannot_be_edited(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.submit_review(db, session, actor="test")  # returns (session, generation_result)
    try:
        wizard_service.save_music_step(
            db, session, music_type=MusicType.own_playlist, notes=None, bump_in_notes=None, actor="test"
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert str(exc) == wizard_service.ALREADY_SUBMITTED_MESSAGE


# --- outside cake permission --------------------------------------------


def test_outside_cake_rejected_when_not_permitted(db, loft):
    booking = _make_booking(db, loft)
    assert booking.outside_cake_permitted is False
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    try:
        wizard_service.save_extras_step(
            db, session, cake_choice_type=CakeChoiceType.outside, cake_menu_item_id=None, cake_notes="a cake",
            decorations_notes=None, layout_notes=None, dietary_requirements=None, accessibility_needs=None,
            additional_notes=None, actor="test",
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_outside_cake_allowed_when_grandfathered(db, loft):
    booking = _make_booking(db, loft)
    booking.outside_cake_permitted = True
    db.commit()
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.outside, cake_menu_item_id=None, cake_notes="GF cake, own table",
        decorations_notes=None, layout_notes=None, dietary_requirements=None, accessibility_needs=None,
        additional_notes=None, actor="test",
    )
    assert session.extras_response["cake_choice"]["type"] == "outside"


# --- accessibility hard escalation ------------------------------------------


def test_accessibility_need_against_loft_triggers_hard_escalation(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    escalated = wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None,
        accessibility_needs="Guest uses a wheelchair", additional_notes=None, actor="test",
    )
    assert escalated is True
    assert session.has_hard_escalation is True
    event_types = {e.event_type for e in booking.events}
    assert "accessibility_escalation" in event_types


def test_accessibility_need_against_mezzanine_triggers_hard_escalation(db, mezzanine):
    booking = _make_booking(db, mezzanine)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    escalated = wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None,
        accessibility_needs="Needs step-free access", additional_notes=None, actor="test",
    )
    assert escalated is True


def test_accessibility_need_against_lounge_never_escalates(db, lounge):
    booking = _make_booking(db, lounge)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    escalated = wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None,
        accessibility_needs="Guest uses a wheelchair", additional_notes=None, actor="test",
    )
    assert escalated is False
    assert session.has_hard_escalation is False


def test_no_accessibility_need_does_not_escalate(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    escalated = wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None,
        accessibility_needs=None, additional_notes=None, actor="test",
    )
    assert escalated is False


# --- eligibility query -------------------------------------------------


def test_eligible_booking_appears_in_worklist(db, hamilton, loft):
    as_of = dt.date(2027, 6, 1)
    booking = _make_eligible_booking(db, loft, event_date=as_of + dt.timedelta(days=14))
    results = wizard_service.get_wizard_eligible_bookings(db, hamilton, as_of=as_of)
    assert booking.id in {b.id for b in results}


def test_unpaid_deposit_excludes_booking(db, hamilton, loft):
    as_of = dt.date(2027, 6, 1)
    booking = _make_booking(db, loft, event_date=as_of + dt.timedelta(days=14))
    agreement = documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    agreement = documents_service.mark_sent(db, agreement, actor="test")
    documents_service.sign(db, agreement, signer_name="Test", signer_ip="127.0.0.1")
    # No deposit invoice at all -- must not appear.
    results = wizard_service.get_wizard_eligible_bookings(db, hamilton, as_of=as_of)
    assert booking.id not in {b.id for b in results}


def test_unsigned_agreement_excludes_booking(db, hamilton, loft):
    as_of = dt.date(2027, 6, 1)
    booking = _make_booking(db, loft, event_date=as_of + dt.timedelta(days=14))
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    invoicing.record_payment(db, invoice, amount=500, method=PaymentMethod.card, actor="test")
    # Agreement created but never signed.
    documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    results = wizard_service.get_wizard_eligible_bookings(db, hamilton, as_of=as_of)
    assert booking.id not in {b.id for b in results}


def test_fourteen_day_boundary(db, hamilton, loft):
    as_of = dt.date(2027, 6, 1)
    booking_13_days = _make_eligible_booking(db, loft, event_date=as_of + dt.timedelta(days=13))
    booking_14_days = _make_eligible_booking(db, loft, event_date=as_of + dt.timedelta(days=14))
    booking_15_days = _make_eligible_booking(db, loft, event_date=as_of + dt.timedelta(days=15))

    ids = {b.id for b in wizard_service.get_wizard_eligible_bookings(db, hamilton, as_of=as_of)}
    assert booking_13_days.id in ids
    assert booking_14_days.id in ids
    assert booking_15_days.id not in ids


def test_already_submitted_wizard_excluded_from_worklist(db, hamilton, loft):
    as_of = dt.date(2027, 6, 1)
    booking = _make_eligible_booking(db, loft, event_date=as_of + dt.timedelta(days=10))
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.submit_review(db, session, actor="test")  # returns (session, generation_result)

    ids = {b.id for b in wizard_service.get_wizard_eligible_bookings(db, hamilton, as_of=as_of)}
    assert booking.id not in ids


# --- token lifecycle, HTTP level ---------------------------------------


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_unknown_token_404s(db):
    client = _client(db)
    try:
        resp = client.get("/w/does-not-exist")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_null_byte_token_404s_not_500(db):
    client = _client(db)
    try:
        resp = client.get("/w/abc%00def")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_revoked_session_404s(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.revoke_session(db, session, actor="test")
    client = _client(db)
    try:
        resp = client.get(f"/w/{session.access_token}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_expired_unsubmitted_session_404s(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    session.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    db.commit()
    client = _client(db)
    try:
        resp = client.get(f"/w/{session.access_token}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_wizard_bootstrap_includes_the_cake_catalogue(db, loft, menu_items):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    client = _client(db)
    try:
        resp = client.get(f"/w/{session.access_token}")
        assert resp.status_code == 200
        assert "Chocolate Mud Cake" in resp.text
        assert "Vanilla Cake (2 Layer)" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_first_open_records_opened_at(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    assert session.opened_at is None

    client = _client(db)
    try:
        client.get(f"/w/{session.access_token}")
        db.refresh(session)
        assert session.opened_at is not None

        first_opened_at = session.opened_at
        client.get(f"/w/{session.access_token}")  # a second visit must not move the timestamp
        db.refresh(session)
        assert session.opened_at == first_opened_at
    finally:
        app.dependency_overrides.clear()


def test_submitted_session_still_resolves_read_only(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.submit_review(db, session, actor="test")  # returns (session, generation_result)
    client = _client(db)
    try:
        resp = client.get(f"/w/{session.access_token}")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_post_to_submitted_session_returns_409(db, loft):
    booking = _make_booking(db, loft)
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    wizard_service.submit_review(db, session, actor="test")  # returns (session, generation_result)
    client = _client(db)
    try:
        resp = client.post(
            f"/w/{session.access_token}/music",
            json={"music_type": "dj", "notes": None, "bump_in_notes": None},
        )
        assert resp.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_basics_step_via_http_returns_warnings(db, loft):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 8, 7))  # a Saturday
    session = wizard_service.get_or_create_session(db, booking, actor="test")
    client = _client(db)
    try:
        resp = client.post(
            f"/w/{session.access_token}/basics",
            json={
                "start_time": "11:00:00",
                "end_time": "18:00:00",  # breaches the Saturday-daytime 5pm rule
                "food_service_time": "12:00:00",
                "setup_access_time": "14:00:00",
                "adult_count": 50,
                "child_count": 5,
            },
        )
        assert resp.status_code == 200
        codes = {w["code"] for w in resp.json()["warnings"]}
        assert "saturday_daytime_finish" in codes
    finally:
        app.dependency_overrides.clear()


# --- signed / deposit-paid alerts fire from the real flows --------------------


def test_signing_agreement_triggers_venue_alert(db, loft, monkeypatch):
    booking = _make_booking(db, loft)
    agreement = documents_service.create_new_version(db, booking, DocumentType.agreement, {"terms": "ok"}, actor="test")
    agreement = documents_service.mark_sent(db, agreement, actor="test")

    calls = []
    monkeypatch.setattr(
        "app.services.notifications.notify_agreement_signed",
        lambda booking, **kw: calls.append(kw),
    )
    documents_service.sign(db, agreement, signer_name="Nicole Jones", signer_ip="127.0.0.1")
    assert len(calls) == 1
    assert calls[0]["signer_name"] == "Nicole Jones"
    assert calls[0]["deposit_paid"] is False  # no deposit yet


def test_paying_deposit_triggers_venue_alert(db, loft, monkeypatch):
    booking = _make_booking(db, loft)
    deposit = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, deposit, actor="test")

    calls = []
    monkeypatch.setattr(
        "app.services.notifications.notify_deposit_paid",
        lambda booking, **kw: calls.append(kw),
    )
    invoicing.record_payment(db, deposit, amount=Decimal("500.00"), method=PaymentMethod.card, actor="test")
    assert len(calls) == 1
    assert calls[0]["amount"] == Decimal("500.00")


def test_partial_deposit_payment_does_not_alert(db, loft, monkeypatch):
    booking = _make_booking(db, loft)
    deposit = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, deposit, actor="test")

    calls = []
    monkeypatch.setattr("app.services.notifications.notify_deposit_paid", lambda booking, **kw: calls.append(kw))
    invoicing.record_payment(db, deposit, amount=Decimal("200.00"), method=PaymentMethod.card, actor="test")
    assert calls == []  # deposit not cleared yet


def test_final_invoice_payment_does_not_trigger_deposit_alert(db, loft, monkeypatch):
    booking = _make_booking(db, loft)
    final = invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Catering", "quantity": 1, "unit_price": "800.00"}],
        due_date=dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, final, actor="test")

    calls = []
    monkeypatch.setattr("app.services.notifications.notify_deposit_paid", lambda booking, **kw: calls.append(kw))
    invoicing.record_payment(db, final, amount=Decimal("800.00"), method=PaymentMethod.card, actor="test")
    assert calls == []  # only the deposit alerts, not a final-invoice payment
