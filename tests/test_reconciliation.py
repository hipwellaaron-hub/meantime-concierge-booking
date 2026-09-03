"""The nightly reconciliation job (brief section 9).

Reads everything, fixes nothing. The two tests that matter most are the
ones about noise: a finding must not be re-raised every night, and it must
close itself when the problem is fixed. A job that cries wolf nightly gets
ignored, and then it is worse than not having it.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.models import Contact, ReconciliationFinding
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.models.wizard_session import WizardSession, WizardSessionStatus
from app.services import documents as documents_service
from app.services import invoicing, reconciliation
from app.services.booking import change_status, create_booking
from app.services.document_generation import generate_agreement_content

TODAY = dt.date.today()


def _contact(db, name="Recon Client", email="recon.client@example.com"):
    c = Contact(name=name, email=email)
    db.add(c)
    db.flush()
    return c


def _booking(db, space, *, name="Recon Test", when=None, contact=None,
             start=dt.time(18, 0), end=dt.time(23, 0)):
    return create_booking(
        db, space_id=space.id,
        contact_id=(contact or _contact(db, email=f"{name.replace(' ', '.').lower()}@example.com")).id,
        event_date=when or TODAY + dt.timedelta(days=60),
        start_time=start, end_time=end, event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="staff:test",
    )


def _codes(findings):
    return {f.check_code for f in findings}


# --- individual checks --------------------------------------------------


def test_confirmed_without_email_is_flagged(db, hamilton, loft):
    b = _booking(db, loft, name="No Email")
    b.contact.email = ""
    change_status(db, b, BookingStatus.confirmed, actor="staff:test")
    assert "CONFIRMED_NO_EMAIL" in _codes(reconciliation.collect(db, hamilton))


def test_tbd_reference_and_far_future_date_are_flagged(db, hamilton, loft):
    b = _booking(db, loft, name="Far Future", when=TODAY + dt.timedelta(days=800))
    b.reference_code = "HAM-TBD-VXW04"
    db.commit()
    codes = _codes(reconciliation.collect(db, hamilton))
    assert "REFERENCE_TBD" in codes
    assert "DATE_TOO_FAR_OUT" in codes


def test_confirmed_without_gates_is_flagged_but_a_pinned_one_is_not(db, hamilton, loft):
    """A pinned status is a deliberate human decision -- Breast Cancer
    Trials, signed with the deposit waived -- not a discrepancy."""
    unpinned = _booking(db, loft, name="Confirmed Bare")
    change_status(db, unpinned, BookingStatus.confirmed, actor="staff:test")
    assert "CONFIRMED_WITHOUT_GATES" in _codes(reconciliation.collect(db, hamilton))

    unpinned.status_pinned_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    assert "CONFIRMED_WITHOUT_GATES" not in _codes(reconciliation.collect(db, hamilton))


def test_overlapping_open_enquiries_are_flagged(db, hamilton, loft):
    """The double-offer risk the exclusion constraint cannot catch,
    because enquiries do not block."""
    when = TODAY + dt.timedelta(days=70)
    _booking(db, loft, name="Party One", when=when)
    _booking(db, loft, name="Party Two", when=when)
    assert "OVERLAPPING_OPEN_INTEREST" in _codes(reconciliation.collect(db, hamilton))


def test_non_overlapping_times_in_one_room_are_not_flagged(db, hamilton, mezzanine):
    when = TODAY + dt.timedelta(days=71)
    _booking(db, mezzanine, name="Lunch", when=when, start=dt.time(11, 30), end=dt.time(15, 0))
    _booking(db, mezzanine, name="Evening", when=when, start=dt.time(18, 0), end=dt.time(23, 30))
    assert "OVERLAPPING_OPEN_INTEREST" not in _codes(reconciliation.collect(db, hamilton))


def test_split_event_across_two_rooms_is_flagged(db, hamilton, loft, mezzanine):
    """One contact, one date, two rooms, not linked -- the Adrienne shape."""
    when = TODAY + dt.timedelta(days=72)
    shared = _contact(db, name="Adrienne Mckinney", email="adrienne@example.com")
    _booking(db, loft, name="Engagement Loft", when=when, contact=shared)
    _booking(db, mezzanine, name="Engagement Mezz", when=when, contact=shared)
    assert "SPLIT_EVENT" in _codes(reconciliation.collect(db, hamilton))


def test_a_properly_linked_two_room_event_is_not_flagged(db, hamilton, loft, mezzanine):
    from app.services.booking import add_linked_space

    when = TODAY + dt.timedelta(days=73)
    parent = _booking(db, loft, name="Linked Properly", when=when)
    add_linked_space(db, parent, space_id=mezzanine.id, actor="staff:test")
    assert "SPLIT_EVENT" not in _codes(reconciliation.collect(db, hamilton))


def test_expired_unsubmitted_wizard_is_flagged(db, hamilton, loft):
    b = _booking(db, loft, name="Wizard Late")
    db.add(
        WizardSession(
            booking_id=b.id,
            status=WizardSessionStatus.in_progress,
            expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2),
        )
    )
    db.commit()
    assert "WIZARD_OVERDUE" in _codes(reconciliation.collect(db, hamilton))


def test_imminent_event_without_a_beo_is_flagged(db, hamilton, loft):
    """The weekend check, done by hand until now."""
    b = _booking(db, loft, name="This Weekend", when=TODAY + dt.timedelta(days=3))
    change_status(db, b, BookingStatus.confirmed, actor="staff:test")
    assert "IMMINENT_NO_BEO" in _codes(reconciliation.collect(db, hamilton))

    beo = documents_service.create_new_version(db, b, DocumentType.beo, {"x": 1}, actor="staff:test")
    documents_service.mark_sent(db, beo, actor="staff:test")
    assert "IMMINENT_NO_BEO" not in _codes(reconciliation.collect(db, hamilton))


def test_doubled_contact_name_and_malformed_email_are_flagged(db, hamilton, loft):
    doubled = _contact(db, name="Christine Hipwell Christine Hipwell", email="ch@example.com")
    _booking(db, loft, name="Doubled Name", contact=doubled)

    typo = _contact(db, name="Emily Marsh", email="emilyleamarsh@gmail.clm")
    _booking(db, loft, name="Typo Email", when=TODAY + dt.timedelta(days=61), contact=typo)

    codes = _codes(reconciliation.collect(db, hamilton))
    assert "CONTACT_NAME_DUPLICATED" in codes
    assert "CONTACT_EMAIL_MALFORMED" in codes


def test_ordinary_contacts_are_not_flagged(db, hamilton, loft):
    fine = _contact(db, name="Sarah Lockwood", email="sarah.lockwood@palaris.com.au")
    _booking(db, loft, name="Perfectly Fine", contact=fine)
    codes = _codes(reconciliation.collect(db, hamilton))
    assert "CONTACT_NAME_DUPLICATED" not in codes
    assert "CONTACT_EMAIL_MALFORMED" not in codes


# --- dedup and auto-clear: the anti-noise behaviour ---------------------


def test_a_finding_is_not_re_raised_every_night(db, hamilton, loft):
    b = _booking(db, loft, name="Persistent Problem")
    b.reference_code = "HAM-TBD-AAAAA"
    db.commit()

    first = reconciliation.run(db, hamilton)
    assert first.opened >= 1

    second = reconciliation.run(db, hamilton)
    assert second.opened == 0, "a surviving problem must not open a second finding"
    assert second.still_open >= 1

    rows = db.query(ReconciliationFinding).filter_by(check_code="REFERENCE_TBD").all()
    assert len(rows) == 1


def test_a_finding_clears_itself_when_the_problem_is_fixed(db, hamilton, loft):
    b = _booking(db, loft, name="Will Be Fixed")
    b.reference_code = "HAM-TBD-BBBBB"
    db.commit()
    reconciliation.run(db, hamilton)

    row = db.query(ReconciliationFinding).filter_by(check_code="REFERENCE_TBD").one()
    assert row.resolved_at is None

    b.reference_code = "HAM-20270101-BBBBB"  # the human fixes it
    db.commit()
    result = reconciliation.run(db, hamilton)
    assert result.resolved >= 1

    db.refresh(row)
    assert row.resolved_at is not None
    assert row not in reconciliation.open_findings(db, hamilton)


def test_a_recurring_problem_reopens_the_same_row(db, hamilton, loft):
    b = _booking(db, loft, name="Recurs")
    b.reference_code = "HAM-TBD-CCCCC"
    db.commit()
    reconciliation.run(db, hamilton)

    b.reference_code = "HAM-20270101-CCCCC"
    db.commit()
    reconciliation.run(db, hamilton)

    b.reference_code = "HAM-TBD-CCCCC"  # back again
    db.commit()
    reconciliation.run(db, hamilton)

    rows = db.query(ReconciliationFinding).filter_by(
        booking_id=b.id, check_code="REFERENCE_TBD"
    ).all()
    assert len(rows) == 1, "a recurrence must reopen the one row, not start a second history"
    assert rows[0].resolved_at is None


def test_the_job_writes_nothing_to_the_bookings_it_inspects(db, hamilton, loft):
    """Reads everything, fixes nothing."""
    b = _booking(db, loft, name="Untouched")
    change_status(db, b, BookingStatus.confirmed, actor="staff:test")
    before = (b.status, b.reference_code, b.event_date, b.contact.email)

    reconciliation.run(db, hamilton)
    db.refresh(b)
    assert (b.status, b.reference_code, b.event_date, b.contact.email) == before


def test_a_clean_venue_produces_no_findings(db, hamilton, loft):
    """Nothing spurious: a well-formed booking raises nothing at all."""
    good = _contact(db, name="Jordyn Slattery", email="jordyn@example.com")
    b = _booking(db, loft, name="All Good", contact=good)
    doc = documents_service.create_new_version(
        db, b, DocumentType.agreement, generate_agreement_content(b), actor="staff:test"
    )
    documents_service.mark_sent(db, doc, actor="staff:test")
    inv = invoicing.create_invoice(
        db, b, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        TODAY, actor="staff:test",
    )
    invoicing.mark_sent(db, inv, actor="staff:test")
    documents_service.sign(db, doc, signer_name="Jordyn", signer_ip="1.2.3.4")
    invoicing.record_payment(
        db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="staff:test"
    )

    assert reconciliation.collect(db, hamilton) == []


# --- linked children are the parent's problem, not their own -------------


def test_a_linked_child_room_does_not_get_its_own_gate_findings(db, hamilton, loft, mezzanine):
    """The Adrienne Mckinney case: one engagement party across the Loft and
    the Mezzanine. The child never owns an agreement or a deposit -- the
    parent does -- so flagging it "confirmed without gates" is a phantom
    that would appear on Triage twice and never clear."""
    from app.services.booking import add_linked_space

    parent = _booking(db, loft, name="Engagement Party", when=TODAY + dt.timedelta(days=75))
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="staff:test")
    change_status(db, parent, BookingStatus.confirmed, actor="staff:test")
    change_status(db, child, BookingStatus.confirmed, actor="staff:test")

    findings = reconciliation.collect(db, hamilton)
    child_findings = [f for f in findings if f.booking_id == child.id]
    assert child_findings == [], [f.check_code for f in child_findings]

    # The parent is still checked -- it genuinely has no gates yet.
    assert any(
        f.booking_id == parent.id and f.check_code == "CONFIRMED_WITHOUT_GATES" for f in findings
    )


def test_a_linked_child_is_not_flagged_for_a_missing_event_order_either(db, hamilton, loft, mezzanine):
    from app.services.booking import add_linked_space

    parent = _booking(db, loft, name="Imminent Two Rooms", when=TODAY + dt.timedelta(days=3))
    child = add_linked_space(db, parent, space_id=mezzanine.id, actor="staff:test")
    change_status(db, parent, BookingStatus.confirmed, actor="staff:test")
    change_status(db, child, BookingStatus.confirmed, actor="staff:test")

    findings = reconciliation.collect(db, hamilton)
    assert not any(f.booking_id == child.id and f.check_code == "IMMINENT_NO_BEO" for f in findings)


# --- a TBD reference resolves itself once the date is known ---------------


def test_setting_a_date_on_a_tbd_booking_regenerates_its_reference(db, hamilton, loft, unassigned_space):
    """Nicole Jones: HAM-TBD-VXW04, confirmed and paid, date wrong. Until
    now a reference was generated once at creation and never touched, so
    correcting the date left the TBD in place and the nightly flag firing
    forever."""
    from app.services.booking import assign_space_and_time

    # Built directly: the _booking helper defaults a missing date to a real
    # one, which would give this a real reference and prove nothing.
    b = create_booking(
        db, space_id=unassigned_space.id, contact_id=_contact(db, email="nicole.xmas@example.com").id,
        event_date=None, start_time=None, end_time=None, event_name="Nicole Xmas",
        event_type="christmas party", adult_count=50, child_count=0, notes=None, actor="staff:test",
    )
    assert "TBD" in b.reference_code
    old_ref = b.reference_code

    assign_space_and_time(
        db, b, space_id=loft.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_date=dt.date(2026, 11, 28), actor="staff:test",
    )
    db.refresh(b)

    assert "TBD" not in b.reference_code
    assert b.reference_code.startswith("HAM-20261128-")
    assert any(
        e.event_type == "field_changed" and e.field_name == "reference_code"
        and e.old_value == old_ref and e.new_value == b.reference_code
        for e in b.events
    ), "the rewrite must leave an audit trail"
    # And the nightly flag now clears on its own.
    assert not any(f.booking_id == b.id and f.check_code == "REFERENCE_TBD"
                   for f in reconciliation.collect(db, hamilton))


def test_a_real_reference_is_never_regenerated(db, hamilton, loft, mezzanine):
    """It is on sent documents and emails. Changing the date must not
    change the reference a client already holds."""
    from app.services.booking import assign_space_and_time

    b = _booking(db, loft, name="Stable Ref", when=TODAY + dt.timedelta(days=60))
    original = b.reference_code
    assert "TBD" not in original

    assign_space_and_time(
        db, b, space_id=mezzanine.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_date=TODAY + dt.timedelta(days=61), actor="staff:test",
    )
    db.refresh(b)
    assert b.reference_code == original


# --- never change a reference the client already holds -------------------


def test_a_tbd_booking_with_a_sent_invoice_keeps_its_reference(db, hamilton, loft, unassigned_space):
    """The Nicole Jones case in full: TBD reference, but an invoice went
    out with it printed on and was paid. Correcting the date must NOT
    regenerate the reference -- Stripe's description of that payment
    carries the old one, and a reference Concierge no longer knows is a
    month-end search, not a tidy-up."""
    from decimal import Decimal

    from app.models.invoice import InvoiceType
    from app.models.payment import PaymentMethod
    from app.services import invoicing
    from app.services.booking import assign_space_and_time

    nicole = create_booking(
        db, space_id=unassigned_space.id, contact_id=_contact(db, email="nicole.paid@example.com").id,
        event_date=None, start_time=None, end_time=None, event_name="Xmas Party",
        event_type="christmas party", adult_count=50, child_count=0, notes=None, actor="staff:test",
    )
    tbd_ref = nicole.reference_code
    assert "TBD" in tbd_ref

    inv = invoicing.create_invoice(
        db, nicole, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}], TODAY, actor="staff:test",
    )
    invoicing.mark_sent(db, inv, actor="staff:test")
    invoicing.record_payment(db, inv, amount=Decimal("509.00"), method=PaymentMethod.card, actor="stripe_webhook")

    assign_space_and_time(
        db, nicole, space_id=loft.id, start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        event_date=dt.date(2026, 11, 28), actor="staff:test",
    )
    db.refresh(nicole)

    assert nicole.reference_code == tbd_ref, "a reference the client holds must never change"
    assert nicole.event_date == dt.date(2026, 11, 28)  # the date itself is still corrected
    assert not any(e.field_name == "reference_code" for e in nicole.events)


def test_a_retained_tbd_reference_is_not_nagged_about_nightly(db, hamilton, loft):
    """Once something has been sent the reference is a decision, not a
    defect. A wrong date on the same booking is still caught."""
    from app.models.invoice import InvoiceType
    from app.services import invoicing

    b = _booking(db, loft, name="Retained TBD", when=TODAY + dt.timedelta(days=800))
    b.reference_code = "HAM-TBD-KEEPS"
    db.commit()
    inv = invoicing.create_invoice(
        db, b, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}], TODAY, actor="staff:test",
    )
    invoicing.mark_sent(db, inv, actor="staff:test")

    codes = _codes([f for f in reconciliation.collect(db, hamilton) if f.booking_id == b.id])
    assert "REFERENCE_TBD" not in codes
    assert "DATE_TOO_FAR_OUT" in codes, "the real error must stay visible"


def test_run_resolves_only_this_venues_findings(db, hamilton, loft):
    """A second venue's open finding must survive a Hamilton run. Loading
    every finding into the resolve loop would have closed it."""
    from sqlalchemy import select

    from app.models import ReconciliationFinding, Space, Venue

    entrance = Venue(name="The Entrance", slug="entrance")
    db.add(entrance)
    db.flush()
    private_bar = Space(venue_id=entrance.id, name="Private Bar", capacity=60,
                        min_food_spend=1000, standard_min_adults=40)
    db.add(private_bar)
    db.flush()

    theirs = _booking(db, private_bar, name="Entrance No Email")
    theirs.contact.email = ""
    change_status(db, theirs, BookingStatus.confirmed, actor="staff:test")
    reconciliation.run(db, entrance)
    row = db.scalar(select(ReconciliationFinding).where(
        ReconciliationFinding.booking_id == theirs.id,
        ReconciliationFinding.check_code == "CONFIRMED_NO_EMAIL"))
    assert row is not None and row.resolved_at is None

    reconciliation.run(db, hamilton)  # sees none of the Entrance's bookings
    db.refresh(row)
    assert row.resolved_at is None, "Hamilton's run closed the Entrance's finding"
