"""iVvy -> Concierge migration importer (app.services.concierge_migration).

Money-critical, so the coverage is deliberately broad: linked-space merge,
deposit read from total_paid (not assumed), Laura's signed-but-unpaid case,
gate satisfaction, no deposit-paid email storm, the excluded/UNKNOWN skips,
idempotency, the email-typo correction, and -- the one duplicate the iVvy
code can't catch -- a hand-entered booking already in Concierge.
"""

import csv
import datetime as dt
from decimal import Decimal

import pytest

from app.models import Booking, Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentStatus, DocumentType
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.services import notifications
from app.services.booking import create_booking, has_paid_deposit, has_signed_agreement
from app.services.concierge_migration import import_migration_csv
from app.services.invoicing import get_deposit_paid

FIELDS = [
    "booking_code", "event_date", "event_name", "event_type", "space", "start_time", "end_time",
    "pax", "status", "contact_name", "contact_phone", "contact_email", "company",
    "opportunity_created", "pricing_locked_at", "lead_source", "food_total", "total_revenue",
    "total_paid", "total_outstanding", "deposit_paid", "beo_number", "coordinator", "layout", "comments",
]


def _row(**over):
    base = dict(
        booking_code="AAA111", event_date="2026-11-14", event_name="Test Party", event_type="Birthday",
        space="The Loft", start_time="18:00", end_time="23:30", pax="60", status="Confirmed",
        contact_name="Sam Jones", contact_phone="+61400111222", contact_email="sam@example.com",
        company="", opportunity_created="2026-03-14", pricing_locked_at="2026-03-14",
        lead_source="Website Booking Engine", food_total="500.00", total_revenue="500.00",
        total_paid="500.00", total_outstanding="0.00", deposit_paid="YES", beo_number="Ungenerated",
        coordinator="Aaron Hipwell", layout="Custom", comments="",
    )
    base.update(over)
    return base


def _write_csv(tmp_path, rows) -> str:
    path = tmp_path / "import.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


def _one(db, code) -> Booking:
    return db.query(Booking).filter_by(migration_source="ivvy", migration_external_ref=code).one()


def test_basic_import_confirmed_real_space_and_pricing_date(db, hamilton, tmp_path):
    res = import_migration_csv(db, _write_csv(tmp_path, [_row(contact_phone="+61,400,111,222")]), venue=hamilton)
    assert len(res.created) == 1 and not res.errors
    b = _one(db, "AAA111")
    assert b.status.value == "confirmed"
    assert b.space.name == "The Loft"
    assert b.event_date == dt.date(2026, 11, 14)
    assert b.start_time == dt.time(18, 0) and b.end_time == dt.time(23, 30)
    assert b.adult_count == 60
    assert b.reference_code.startswith("HAM-")
    # pricing date from Opportunity Created, NOT today
    assert b.pricing_locked_at == dt.date(2026, 3, 14)
    # negotiated minimum defaults to the space standard (corrected by hand later)
    assert b.agreed_min_adults == b.space.standard_min_adults
    # phone comma-mangling cleaned
    assert b.contact.phone == "+61400111222"


def test_linked_two_room_booking_merges_with_one_deposit(db, hamilton, tmp_path):
    rows = [
        _row(booking_code="LINK1", space="The Loft", pax="60", total_paid="1000.00"),
        _row(booking_code="LINK1", space="The Mezzanine", pax="40", total_paid="1000.00"),
    ]
    res = import_migration_csv(db, _write_csv(tmp_path, rows), venue=hamilton)
    assert len(res.created) == 1 and not res.errors
    parent = _one(db, "LINK1")
    assert parent.parent_booking_id is None and parent.space.name == "The Loft"
    children = parent.linked_bookings
    assert len(children) == 1
    assert children[0].space.name == "The Mezzanine"
    assert children[0].adult_count == 40  # the linked room's own pax, not the parent's 60
    # ONE deposit of $1000 on the parent (not two, not $500)
    deposits = db.query(Invoice).filter_by(booking_id=parent.id, type=InvoiceType.deposit).all()
    assert len(deposits) == 1 and deposits[0].total == Decimal("1000.00")
    assert get_deposit_paid(db, parent) == Decimal("1000.00")


def test_deposit_reads_total_paid_two_room_can_be_500(db, hamilton, tmp_path):
    # Adrienne's real case: two rooms but only a single $500 deposit collected.
    rows = [
        _row(booking_code="ADRI1", space="The Loft", pax="100", total_paid="500.00", total_outstanding="1000.00"),
        _row(booking_code="ADRI1", space="The Mezzanine", pax="0", total_paid="500.00", total_outstanding="1000.00"),
    ]
    import_migration_csv(db, _write_csv(tmp_path, rows), venue=hamilton)
    parent = _one(db, "ADRI1")
    assert get_deposit_paid(db, parent) == Decimal("500.00")
    assert parent.linked_bookings[0].adult_count == 0  # empty second room is a faithful artefact


def test_laura_due_signed_agreement_deposit_outstanding(db, hamilton, tmp_path):
    row = _row(
        booking_code="LAURA1", deposit_paid="DUE", total_paid="", total_outstanding="",
        opportunity_created="", pricing_locked_at="",
        comments="Agreement signed 31/08/2026. Deposit due 03/09/2026.",
    )
    res = import_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)
    b = _one(db, "LAURA1")
    assert has_signed_agreement(db, b) is True
    assert get_deposit_paid(db, b) == Decimal("0.00")  # outstanding, not paid
    dep = db.query(Invoice).filter_by(booking_id=b.id, type=InvoiceType.deposit).one()
    assert dep.status == InvoiceStatus.sent and dep.paid_at is None
    agreement = next(d for d in b.documents if d.type == DocumentType.agreement)
    assert agreement.signed_at.date() == dt.date(2026, 8, 31)
    assert any("defaulted to import date" in fl for fl in res.created[0].flags)


def test_paid_deposit_sends_no_notifications(db, hamilton, tmp_path, monkeypatch):
    """The whole reason deposits are built directly: a bulk migration must
    not fire the per-payment 'deposit paid' (or 'agreement signed') email."""
    calls = []
    monkeypatch.setattr(notifications, "notify_deposit_paid", lambda *a, **k: calls.append("deposit"))
    monkeypatch.setattr(notifications, "notify_agreement_signed", lambda *a, **k: calls.append("agreement"))
    import_migration_csv(db, _write_csv(tmp_path, [_row()]), venue=hamilton)
    assert calls == []


def test_gates_satisfied_for_paid_booking(db, hamilton, tmp_path):
    import_migration_csv(db, _write_csv(tmp_path, [_row()]), venue=hamilton)
    b = _one(db, "AAA111")
    assert has_signed_agreement(db, b) is True
    assert has_paid_deposit(db, b) is True


def test_unknown_deposit_is_skipped(db, hamilton, tmp_path):
    res = import_migration_csv(db, _write_csv(tmp_path, [_row(booking_code="UNK1", deposit_paid="UNKNOWN")]), venue=hamilton)
    assert res.skipped_unknown == ["UNK1"]
    assert db.query(Booking).filter_by(migration_external_ref="UNK1").count() == 0


def test_excluded_code_never_imports_even_with_full_data(db, hamilton, tmp_path):
    # One of the four this-weekend codes, but with a complete paid row -- the
    # code exclusion must still refuse it, so a reissued file can't sneak it in.
    row = _row(booking_code="9LNFYZDXY2", deposit_paid="YES", total_paid="500.00")
    res = import_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)
    assert res.skipped_excluded == ["9LNFYZDXY2"]
    assert db.query(Booking).filter_by(migration_external_ref="9LNFYZDXY2").count() == 0


def test_idempotent_rerun_skips_by_code(db, hamilton, tmp_path):
    path = _write_csv(tmp_path, [_row()])
    import_migration_csv(db, path, venue=hamilton)
    res2 = import_migration_csv(db, path, venue=hamilton)
    assert res2.skipped_existing == ["AAA111"] and not res2.created
    assert db.query(Booking).filter_by(migration_external_ref="AAA111").count() == 1


def test_known_email_typo_corrected_and_flagged(db, hamilton, tmp_path):
    row = _row(booking_code="EMAIL1", contact_email="emilyleamarsh@gmail.clm")
    res = import_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)
    b = _one(db, "EMAIL1")
    assert b.contact.email == "emilyleamarsh@gmail.com"
    assert any("corrected" in fl for fl in res.created[0].flags)


def test_unfixable_malformed_email_is_refused(db, hamilton, tmp_path):
    res = import_migration_csv(db, _write_csv(tmp_path, [_row(booking_code="BADEML", contact_email="not-an-email")]), venue=hamilton)
    assert not res.created
    assert res.errors and res.errors[0][0] == "BADEML"
    assert db.query(Booking).filter_by(migration_external_ref="BADEML").count() == 0


def test_hand_entered_duplicate_is_skipped_not_duplicated(db, hamilton, tmp_path):
    """The duplicate the iVvy code cannot catch: a booking already entered by
    hand (no migration code) for the same person and date. Must be skipped
    for manual reconciliation, never imported as a second copy."""
    loft = next(s for s in hamilton.spaces if s.name == "The Loft")
    contact = Contact(name="Twin Client", email="twin@example.com", phone=None)
    db.add(contact)
    db.flush()
    create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 11, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name="Hand entered",
        event_type="Birthday", adult_count=60, child_count=0, notes=None, actor="staff",
        status=BookingStatus.confirmed,
    )

    row = _row(booking_code="TWIN1", event_date="2026-11-14", contact_email="twin@example.com", space="The Mezzanine")
    res = import_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)

    assert res.skipped_possible_duplicate and "TWIN1" in res.skipped_possible_duplicate[0]
    assert not res.created
    # only the hand-entered one exists for that contact+date; no migrated twin
    assert db.query(Booking).filter_by(migration_external_ref="TWIN1").count() == 0
    assert db.query(Booking).join(Contact).filter(Contact.email == "twin@example.com").count() == 1


def test_report_is_read_only_and_flags_hand_entered_duplicate(db, hamilton, loft, tmp_path):
    """The pre-import report writes NOTHING and flags exactly what the import
    would skip as a hand-entered duplicate."""
    from app.services.concierge_migration import report_migration_csv

    contact = Contact(name="Twin Client", email="twin@example.com", phone=None)
    db.add(contact)
    db.flush()
    create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 11, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name="Hand entered",
        event_type="Birthday", adult_count=60, child_count=0, notes=None, actor="staff",
        status=BookingStatus.confirmed,
    )
    before = db.query(Booking).count()

    row = _row(booking_code="TWIN1", event_date="2026-11-14", contact_email="twin@example.com", space="The Mezzanine")
    rep = report_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)

    assert any("TWIN1" in n for n in rep.possible_duplicate)
    assert not any(w["code"] == "TWIN1" for w in rep.would_create)
    assert db.query(Booking).count() == before  # read-only: nothing written


def test_exclusion_constraint_backstops_same_slot_different_contact(db, hamilton, tmp_path):
    """If the twin was entered under a different email (so the email/date
    check misses it) but occupies the same space+time, the Postgres
    exclusion constraint refuses the import row -- reported, never duplicated."""
    loft = next(s for s in hamilton.spaces if s.name == "The Loft")
    from app.models.booking import BookingStatus

    contact = Contact(name="Other Name", email="other@example.com", phone=None)
    db.add(contact)
    db.flush()
    create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2026, 11, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name="Same slot",
        event_type="Birthday", adult_count=60, child_count=0, notes=None, actor="staff",
        status=BookingStatus.confirmed,
    )
    row = _row(booking_code="SLOT1", event_date="2026-11-14", space="The Loft",
               start_time="18:00", end_time="23:30", contact_email="different@example.com")
    res = import_migration_csv(db, _write_csv(tmp_path, [row]), venue=hamilton)
    assert not res.created
    assert res.errors and res.errors[0][0] == "SLOT1"
    assert db.query(Booking).filter_by(migration_external_ref="SLOT1").count() == 0
