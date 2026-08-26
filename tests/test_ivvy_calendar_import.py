import datetime as dt

from app.models import Booking
from app.models.booking import BookingStatus
from app.services.ivvy_calendar_import import import_calendar_rows


def _row(**overrides):
    row = {
        "Date": "02/10/2026",
        "Booking Code": "ABC123",
        "Booking Name": "Reuben's 18th",
        "Start Time": "6:00 pm",
        "End Time": "11:30 pm",
        "Space": "The Loft",
        "Status": "Confirmed",
        "Pax": "130",
        "Contact Name": "Reuben Pedonese",
        "Contact Phone": "0491710339",
        "Company Name": None,
        "Booked By": "Aaron Hipwell",
        "BEO#": None,
        "Comments": None,
        "Amount": "0",
        "Total Amount": "0",
    }
    row.update(overrides)
    return row


def test_creates_a_booking_in_its_real_space_and_time(db, hamilton, loft):
    result = import_calendar_rows(db, [_row()], venue=hamilton)

    assert result.created == 1
    booking = db.query(Booking).filter_by(migration_external_ref="ABC123").one()
    assert booking.space_id == loft.id
    assert booking.status == BookingStatus.confirmed
    assert booking.event_date == dt.date(2026, 10, 2)
    assert booking.start_time == dt.time(18, 0)
    assert booking.end_time == dt.time(23, 30)
    assert booking.adult_count == 130
    assert booking.contact_id is None
    assert booking.migration_source == "ivvy_calendar"


def test_contact_details_land_in_notes_not_a_contact_record(db, hamilton, loft):
    import_calendar_rows(db, [_row(**{"Contact Name": "Reuben Pedonese", "Contact Phone": "0491710339"})], venue=hamilton)

    booking = db.query(Booking).filter_by(migration_external_ref="ABC123").one()
    assert "Reuben Pedonese" in booking.notes
    assert "0491710339" in booking.notes
    assert "no email captured" in booking.notes.lower()


def test_is_idempotent_on_booking_code(db, hamilton, loft):
    first = import_calendar_rows(db, [_row()], venue=hamilton)
    second = import_calendar_rows(db, [_row()], venue=hamilton)

    assert first.created == 1
    assert second.created == 0
    assert second.skipped_existing == 1
    assert db.query(Booking).filter_by(migration_external_ref="ABC123").count() == 1


def test_skips_row_with_no_booking_code(db, hamilton, loft):
    blank_row = _row(**{"Booking Code": None, "Booking Name": None, "Status": None, "Contact Name": None})

    result = import_calendar_rows(db, [blank_row], venue=hamilton)

    assert result.created == 0
    assert result.errors == []


def test_unrecognized_status_is_reported_not_guessed(db, hamilton, loft):
    result = import_calendar_rows(db, [_row(Status="Tentative Hold")], venue=hamilton)

    assert result.created == 0
    assert len(result.errors) == 1
    assert "unrecognized status" in result.errors[0].reason


def test_unknown_space_is_reported_not_guessed(db, hamilton):
    result = import_calendar_rows(db, [_row(Space="The Ballroom")], venue=hamilton)

    assert result.created == 0
    assert len(result.errors) == 1
    assert "no real bookable space" in result.errors[0].reason


def test_duplicate_code_creates_parent_plus_linked_child(db, hamilton, loft, mezzanine):
    rows = [
        _row(**{"Space": "The Mezzanine", "Pax": None}),
        _row(**{"Space": "The Loft", "Pax": "100"}),
    ]

    result = import_calendar_rows(db, rows, venue=hamilton)

    assert result.created == 1
    assert result.linked_spaces_added == 1
    parent = db.query(Booking).filter_by(migration_external_ref="ABC123", parent_booking_id=None).one()
    assert parent.space_id == loft.id  # the row with real Pax became the parent
    assert parent.adult_count == 100
    child = db.query(Booking).filter_by(parent_booking_id=parent.id).one()
    assert child.space_id == mezzanine.id
    assert child.event_date == parent.event_date


def test_a_real_double_booking_conflict_is_reported_and_does_not_abort_the_batch(db, hamilton, loft):
    from app.services.booking import create_booking

    create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2026, 10, 2),
        start_time=dt.time(18, 0), end_time=dt.time(23, 30), event_name="Already Confirmed",
        event_type=None, adult_count=50, child_count=0, notes=None, actor="test",
        status=BookingStatus.confirmed,
    )

    conflicting_row = _row(**{"Booking Code": "CONFLICT1"})
    other_row = _row(**{"Booking Code": "FINE1", "Date": "03/10/2026"})

    result = import_calendar_rows(db, [conflicting_row, other_row], venue=hamilton)

    assert result.created == 1
    assert db.query(Booking).filter_by(migration_external_ref="FINE1").count() == 1
    assert db.query(Booking).filter_by(migration_external_ref="CONFLICT1").count() == 0
    assert len(result.errors) == 1
    assert result.errors[0].booking_code == "CONFLICT1"


def test_dedup_matches_a_booking_already_imported_by_the_other_ivvy_importer(db, hamilton, loft):
    """The same real booking can reach Concierge through either iVvy
    export -- app.services.ivvy_import's structured CSV, or this
    calendar export. They must never both create a row for the same
    iVvy Booking Code, regardless of which one got there first."""
    from app.services.booking import create_booking

    create_booking(
        db, space_id=loft.id, contact_id=None, event_date=dt.date(2026, 10, 2),
        event_name="Already imported via the other pipeline", event_type=None,
        adult_count=130, child_count=0, notes=None, actor="test",
        status=BookingStatus.confirmed, migration_source="ivvy", migration_external_ref="ABC123",
    )

    result = import_calendar_rows(db, [_row()], venue=hamilton)

    assert result.created == 0
    assert result.skipped_existing == 1
    assert result.errors == []
    assert db.query(Booking).filter_by(migration_external_ref="ABC123").count() == 1


def test_pax_missing_defaults_to_zero_not_a_crash(db, hamilton, loft):
    result = import_calendar_rows(db, [_row(Pax=None)], venue=hamilton)

    assert result.created == 1
    booking = db.query(Booking).filter_by(migration_external_ref="ABC123").one()
    assert booking.adult_count == 0
