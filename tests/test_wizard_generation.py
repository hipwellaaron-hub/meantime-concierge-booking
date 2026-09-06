import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus
from app.models.invoice import InvoiceStatus, InvoiceType
from app.models.payment import PaymentMethod
from app.services import invoicing
from app.services import wizard as wizard_service
from app.services import wizard_generation
from app.services.booking import change_status, create_booking
from app.services.wizard import BarStructure, CakeChoiceType, MusicType


def _make_booking(db, space, *, event_date=dt.date(2027, 3, 6), adult_count=50, created_at=None):
    contact = Contact(name="Generation Test Contact", email="generation.test@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Generation Test Booking",
        event_type="birthday", adult_count=adult_count, child_count=0, notes=None, actor="test",
    )
    if created_at is not None:
        booking.created_at = created_at
        booking.pricing_locked_at = created_at.date()
        db.commit()
        db.refresh(booking)
    return booking


def _pay_deposit(db, booking, *, amount=Decimal("500.00")):
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit, [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="test",
    )
    invoicing.mark_sent(db, invoice, actor="test")
    if amount > 0:
        invoicing.record_payment(db, invoice, amount=amount, method=PaymentMethod.card, actor="test")
    return invoice


def _complete_all_steps(db, session, menu_items, *, accessibility_needs=None):
    grazing = menu_items["Grazing Platter"]
    basics_booking = session.booking
    wizard_service.save_basics_step(
        db, session, start_time=basics_booking.start_time, end_time=basics_booking.end_time,
        food_service_time=dt.time(18, 30), setup_access_time=dt.time(14, 0),
        adult_count=basics_booking.adult_count, child_count=0, actor="test",
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
    # An empty vendors list is a real "no vendors" answer -- the step is
    # complete without any vendor rows.
    wizard_service.save_vendors_step(db, session, vendors=[], actor="test")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes="No special layout", dietary_requirements=None,
        accessibility_needs=accessibility_needs, additional_notes=None, actor="test",
    )
    # The AV step only exists for Loft bookings; a completeness check that
    # flagged a skipped AV step on any other space would be flagging a
    # step the client never saw.
    if session.booking.space.name == "The Loft":
        wizard_service.save_av_step(
            db, session, video_slideshow=False, microphones_for_speeches=False, notes=None, actor="test"
        )


def test_surcharge_applied_to_gross_subtotal_not_net_of_deposit(db, loft, menu_items, public_holidays):
    # Australia Day 2027-01-26 is a real seeded public holiday (10% surcharge).
    booking = _make_booking(db, loft, event_date=dt.date(2027, 1, 26))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)

    session, result = wizard_service.submit_review(db, session, actor="test")

    # 2 Grazing Platters @ $250 = $500 gross subtotal.
    assert result.invoice.subtotal == Decimal("500.00")
    # Surcharge must be 10% of the GROSS $500, not of ($500 - $500 deposit = $0).
    assert result.invoice.surcharge == Decimal("50.00")
    # Total = gross (500 + 50 surcharge) - 500 deposit credited = 50.
    assert result.invoice.total == Decimal("50.00")


def test_deposit_paid_and_balance_due_agree_between_invoice_and_beo(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)
    session, result = wizard_service.submit_review(db, session, actor="test")

    beo_spend = result.document.content["total_food_spend"]
    # BEO "total" is gross (subtotal + surcharge), matching Master Policy
    # §7's "total, less deposit paid, final balance" framing.
    assert Decimal(beo_spend["total"]) == result.invoice.subtotal + result.invoice.surcharge
    assert Decimal(beo_spend["deposit_paid"]) == Decimal("500.00")
    assert Decimal(beo_spend["balance_due"]) == result.invoice.total


def test_partial_deposit_payment_credits_real_amount_not_assumed_constant(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("300.00"))  # only part of the $500 deposit paid

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)
    session, result = wizard_service.submit_review(db, session, actor="test")

    # Gross = 500 subtotal + 0 surcharge (not a public holiday) = 500.
    # Credited = the real 300 paid, not the theoretical 500 deposit amount.
    assert result.invoice.total == Decimal("500.00") - Decimal("300.00")


def test_unpriced_legacy_pizza_excluded_from_invoice_and_beo_flagged_not_embedded(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 6, 1), created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    vegetarian = menu_items["Vegetarian Pizza"]
    wizard_service.save_basics_step(
        db, session, start_time=dt.time(18, 0), end_time=dt.time(23, 0), food_service_time=dt.time(18, 30),
        setup_access_time=dt.time(14, 0), adult_count=50, child_count=0, actor="test",
    )
    wizard_service.save_food_step(
        db, session, platters=[], pizzas=[{"menu_item_id": vegetarian.id, "quantity": 2}], actor="test"
    )
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="test"
    )
    wizard_service.save_music_step(db, session, music_type=MusicType.dj, notes=None, bump_in_notes=None, actor="test")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes=None, dietary_requirements=None, accessibility_needs=None,
        additional_notes=None, actor="test",
    )

    session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is False
    assert any("Vegetarian Pizza" in item for item in result.outstanding_items)
    # Excluded from the priced invoice line items entirely -- never guessed.
    descriptions = [li["description"] for li in result.invoice.line_items]
    assert "Vegetarian Pizza" not in descriptions
    # And never embedded as inline review commentary inside the BEO content
    # itself (Master Policy §7) -- the food_order line items are clean.
    beo_food_lines = result.document.content["food_order"]["line_items"]
    assert all("REVIEW" not in str(li) for li in beo_food_lines)
    assert all("Vegetarian" not in str(li) for li in beo_food_lines)


def test_accessibility_escalation_blocks_clean_even_with_auto_route_on(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items, accessibility_needs="Guest uses a wheelchair")
    assert session.has_hard_escalation is True

    with patch.object(wizard_generation.settings, "wizard_beo_auto_finalize", True), \
         patch.object(wizard_generation.settings, "wizard_invoice_auto_send", True):
        session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is False
    assert result.document.status == DocumentStatus.draft
    assert result.invoice.status == InvoiceStatus.draft


def test_incomplete_submission_never_clean(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 2}], pizzas=[], actor="test"
    )
    # Beverage, Music, Extras all skipped entirely.
    session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is False
    assert "Beverage step was not completed" in result.outstanding_items
    assert "Music step was not completed" in result.outstanding_items
    assert "Extras step was not completed" in result.outstanding_items


def test_clean_submission_with_flags_off_stays_draft(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)
    # Default config: both auto-route flags are False.
    session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is True
    assert result.document.status == DocumentStatus.draft
    assert result.invoice.status == InvoiceStatus.draft


def test_clean_submission_with_flags_on_gets_sent(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)

    with patch.object(wizard_generation.settings, "wizard_beo_auto_finalize", True), \
         patch.object(wizard_generation.settings, "wizard_invoice_auto_send", True):
        session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is True
    assert result.document.status == DocumentStatus.sent
    assert result.invoice.status == InvoiceStatus.sent


def test_dirty_submission_stays_draft_even_with_flags_on(db, loft, menu_items):
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    grazing = menu_items["Grazing Platter"]
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 2}], pizzas=[], actor="test"
    )
    # Beverage/Music/Extras skipped -- guaranteed dirty.

    with patch.object(wizard_generation.settings, "wizard_beo_auto_finalize", True), \
         patch.object(wizard_generation.settings, "wizard_invoice_auto_send", True):
        session, result = wizard_service.submit_review(db, session, actor="test")

    assert result.is_clean is False
    assert result.document.status == DocumentStatus.draft
    assert result.invoice.status == InvoiceStatus.draft


def test_wizard_submission_reuses_existing_manual_final_invoice_not_a_duplicate(db, loft, menu_items):
    # Staff invoiced this booking by hand before the client got around to
    # completing the wizard -- a real, expected sequence now that a manual
    # final invoice path exists (app.services.invoicing.create_final_invoice).
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking, amount=Decimal("500.00"))

    manual_invoice = invoicing.create_final_invoice(
        db, booking, line_items=[{"description": "Catering", "quantity": 1, "unit_price": "600.00"}],
        due_date=dt.date(2027, 3, 6), actor="staff:aaron",
    )

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)
    session, result = wizard_service.submit_review(db, session, actor="test")

    # Reused, not duplicated.
    assert result.invoice.id == manual_invoice.id
    assert len([i for i in booking.invoices if i.type == InvoiceType.final]) == 1
    # A collision like this is never "clean" -- it must always surface for
    # a human to reconcile, not silently pass through even with auto-route
    # flags on.
    assert result.is_clean is False
    assert any("already exists" in item for item in result.outstanding_items)
