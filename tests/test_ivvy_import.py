import datetime as dt

from app.models import Booking, Space
from app.services.ivvy_import import get_unassigned_bookings, import_ivvy_csv

HEADER = (
    "Code,Purchase Order#,Booking Name,Event Start Date,Event End Date,Status,Coordinator,"
    "Sales Person,Main Contact,Company,Total Attendees Guaranteed,Total Amount,Agent,Agent Contact,"
    "Email,First Name,Last Name,Total Tax Included,Total Paid,Total Outstanding,Cancel Reason,"
    "Last Modified,BCC Opportunity Email Address,Type,Cut Off Date,Cancellation Date,Charging Method,"
    "Guarantee Required,Block ID,Folio ID,Auto Invoicing,\n"
)


def _row(
    code="ABC123",
    name="30th Birthday",
    start="Saturday, 15 August 2026",
    end="Saturday, 15 August 2026",
    status="Confirmed",
    contact="Sam Taylor",
    email="sam@example.com",
    attendees="70",
    total="1723.00",
    paid="500",
    outstanding="1223",
):
    return (
        f'{code},,{name},"{start}","{end}",{status},Aaron Hipwell,Aaron Hipwell,{contact},,{attendees},'
        f"{total},,,{email},,,156.64,{paid},{outstanding},,05/08/2026 7:39 AM,"
        f"lead-84157-x@emails.ivvy.com,Simple,,,,No,,,,\n"
    )


def _write_csv(tmp_path, rows, filename="export.csv"):
    path = tmp_path / filename
    path.write_text(HEADER + "".join(rows), encoding="utf-8-sig")
    return str(path)


def test_import_creates_booking_in_unassigned_space(db, hamilton, tmp_path):
    csv_path = _write_csv(tmp_path, [_row()])
    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 1
    assert result.errors == []

    booking = db.query(Booking).filter_by(migration_external_ref="ABC123").one()
    assert booking.status.value == "confirmed"
    assert booking.event_date == dt.date(2026, 8, 15)
    assert booking.start_time is None
    assert booking.adult_count == 70
    assert booking.migration_source == "ivvy"
    assert booking.migration_snapshot["total_paid"] == "500"
    assert booking.migration_snapshot["total_outstanding"] == "1223"

    space = db.get(Space, booking.space_id)
    assert space.is_bookable is False
    assert space.name == "Unassigned (pending triage)"


def test_import_is_idempotent(db, hamilton, tmp_path):
    csv_path = _write_csv(tmp_path, [_row()])
    import_ivvy_csv(db, csv_path, venue=hamilton)
    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 0
    assert result.skipped_existing == 1
    assert db.query(Booking).filter_by(migration_external_ref="ABC123").count() == 1


def test_unrecognized_status_fails_loud_not_guessed(db, hamilton, tmp_path):
    csv_path = _write_csv(tmp_path, [_row(code="XYZ999", status="Enquiry")])
    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 0
    assert len(result.errors) == 1
    assert "unrecognized status" in result.errors[0].reason
    assert db.query(Booking).filter_by(migration_external_ref="XYZ999").count() == 0


def test_repeat_email_across_rows_reuses_contact(db, hamilton, tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [
            _row(code="A1", email="repeat@example.com", contact="Jamie Lee"),
            _row(code="A2", email="repeat@example.com", contact="Jamie Lee"),
        ],
    )
    import_ivvy_csv(db, csv_path, venue=hamilton)

    b1 = db.query(Booking).filter_by(migration_external_ref="A1").one()
    b2 = db.query(Booking).filter_by(migration_external_ref="A2").one()
    assert b1.contact_id == b2.contact_id


def test_unassigned_bookings_excluded_from_real_availability(db, hamilton, loft, tmp_path):
    from app.services.availability import get_space_candidates

    csv_path = _write_csv(tmp_path, [_row()])
    import_ivvy_csv(db, csv_path, venue=hamilton)

    candidates = get_space_candidates(
        db, hamilton.id, dt.date(2026, 8, 15), dt.time(12, 0), dt.time(16, 0), guest_count=10
    )
    space_names = {c["space"].name for c in candidates}
    assert "Unassigned (pending triage)" not in space_names
    assert "The Loft" in space_names


def test_get_unassigned_bookings_lists_imports_for_triage(db, hamilton, tmp_path):
    csv_path = _write_csv(tmp_path, [_row(code="B1", start="Saturday, 15 August 2026", end="Saturday, 15 August 2026")])
    import_ivvy_csv(db, csv_path, venue=hamilton)

    worklist = get_unassigned_bookings(db, hamilton)
    assert len(worklist) == 1
    assert worklist[0].migration_external_ref == "B1"


def test_get_unassigned_bookings_excludes_terminal_statuses(db, hamilton, tmp_path):
    """A cancelled, dead, or archived import has nothing left to triage --
    it must not linger on this worklist forever just because it was never
    assigned a real space before being closed out."""
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    csv_path = _write_csv(tmp_path, [_row(code="B2", start="Saturday, 15 August 2026", end="Saturday, 15 August 2026")])
    import_ivvy_csv(db, csv_path, venue=hamilton)
    booking = get_unassigned_bookings(db, hamilton)[0]

    change_status(db, booking, BookingStatus.archived, actor="test")

    assert get_unassigned_bookings(db, hamilton) == []


def test_multiday_row_is_skipped_not_guessed(db, hamilton, tmp_path):
    csv_path = _write_csv(
        tmp_path, [_row(code="MULTI", start="Friday, 14 August 2026", end="Saturday, 15 August 2026")]
    )
    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 0
    assert "multi-day" in result.errors[0].reason


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_truncated_row_does_not_crash_the_batch(db, hamilton, tmp_path):
    """Regression: csv.DictReader fills missing trailing fields with None
    (not a KeyError) for a row shorter than the header. .strip() on None
    used to raise an uncaught AttributeError and crash the whole import,
    not just that row."""
    path = tmp_path / "export.csv"
    path.write_text(HEADER + "SHORTROW,,Test Event\n" + _row(code="GOODROW"), encoding="utf-8-sig")

    result = import_ivvy_csv(db, str(path), venue=hamilton)

    assert result.created == 1  # GOODROW still imports
    assert any(e.code == "SHORTROW" for e in result.errors)
    assert db.query(Booking).filter_by(migration_external_ref="GOODROW").count() == 1


def test_missing_column_entirely_reported_not_crashing(db, hamilton, tmp_path):
    """A CSV exported with a different/renamed column set (a future iVvy
    format change) must degrade to per-row errors, not kill the import."""
    bad_header = "Code,Booking Name,Event Start Date,Event End Date\n"
    bad_row = 'XYZ1,Some Event,"Saturday, 15 August 2026","Saturday, 15 August 2026"\n'
    path = tmp_path / "malformed.csv"
    path.write_text(bad_header + bad_row, encoding="utf-8-sig")

    result = import_ivvy_csv(db, str(path), venue=hamilton)

    assert result.created == 0
    assert len(result.errors) == 1
    assert db.query(Booking).filter_by(migration_external_ref="XYZ1").count() == 0


def test_oversized_booking_name_is_truncated_not_fatal(db, hamilton, tmp_path):
    huge_name = "A" * 2000
    csv_path = _write_csv(tmp_path, [_row(code="HUGE", name=huge_name)])

    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 1
    booking = db.query(Booking).filter_by(migration_external_ref="HUGE").one()
    assert len(booking.event_name) <= 255


def test_oversized_contact_name_is_truncated_not_fatal(db, hamilton, tmp_path):
    huge_contact = "B" * 2000
    csv_path = _write_csv(tmp_path, [_row(code="HUGECONTACT", contact=huge_contact)])

    result = import_ivvy_csv(db, csv_path, venue=hamilton)

    assert result.created == 1
    booking = db.query(Booking).filter_by(migration_external_ref="HUGECONTACT").one()
    from app.models import Contact

    contact = db.get(Contact, booking.contact_id)
    assert len(contact.name) <= 255


def test_nonexistent_csv_file_raises_clear_error_not_silent_noop(db, hamilton, tmp_path):
    missing_path = str(tmp_path / "does-not-exist.csv")
    try:
        import_ivvy_csv(db, missing_path, venue=hamilton)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_empty_csv_file_imports_nothing_without_crashing(db, hamilton, tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text(HEADER, encoding="utf-8-sig")  # header only, zero data rows

    result = import_ivvy_csv(db, str(path), venue=hamilton)

    assert result.created == 0
    assert result.errors == []
