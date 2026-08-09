from app.services.ivvy_import import import_ivvy_csv
from app.services.ivvy_reconciliation import reconcile
from tests.test_ivvy_import import HEADER, _row, _write_csv


def test_clean_reconciliation_when_nothing_changed(db, hamilton, tmp_path):
    csv_path = _write_csv(tmp_path, [_row(code="A1")])
    import_ivvy_csv(db, csv_path, venue=hamilton)

    report = reconcile(db, csv_path, venue=hamilton)

    assert report.is_clean is True
    assert report.matched_clean == 1
    assert report.divergences == []
    assert report.new_in_ivvy == []


def test_flags_new_booking_not_yet_imported(db, hamilton, tmp_path):
    original = _write_csv(tmp_path, [_row(code="A1")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    updated = _write_csv(tmp_path, [_row(code="A1"), _row(code="A2")], filename="updated.csv")
    report = reconcile(db, updated, venue=hamilton)

    assert report.is_clean is False
    assert report.new_in_ivvy == ["A2"]


def test_flags_attendee_count_divergence(db, hamilton, tmp_path):
    original = _write_csv(tmp_path, [_row(code="A1", attendees="70")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    updated = _write_csv(tmp_path, [_row(code="A1", attendees="75")], filename="updated.csv")
    report = reconcile(db, updated, venue=hamilton)

    assert report.is_clean is False
    divergence = next(d for d in report.divergences if d.field == "attendee_count")
    assert divergence.concierge_value == "70"
    assert divergence.ivvy_value == "75"


def test_status_change_to_unrecognized_status_is_flagged(db, hamilton, tmp_path):
    """The single most important case: a booking that Concierge still
    thinks is confirmed, but iVvy now shows as Cancelled (a status this
    build has never seen mapped) -- must not be silently skipped."""
    original = _write_csv(tmp_path, [_row(code="A1", status="Confirmed")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    updated = _write_csv(tmp_path, [_row(code="A1", status="Cancelled")], filename="updated.csv")
    report = reconcile(db, updated, venue=hamilton)

    assert report.is_clean is False
    divergence = next(d for d in report.divergences if d.field == "status")
    assert divergence.concierge_value == "confirmed"
    assert "Cancelled" in divergence.ivvy_value


def test_flags_financial_divergence(db, hamilton, tmp_path):
    original = _write_csv(tmp_path, [_row(code="A1", paid="500", outstanding="1223")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    updated = _write_csv(tmp_path, [_row(code="A1", paid="1723", outstanding="0")], filename="updated.csv")
    report = reconcile(db, updated, venue=hamilton)

    assert report.is_clean is False
    fields = {d.field for d in report.divergences}
    assert "total_paid" in fields
    assert "total_outstanding" in fields


def test_missing_from_export_does_not_break_clean_status(db, hamilton, tmp_path):
    original = _write_csv(tmp_path, [_row(code="A1"), _row(code="A2")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    # A narrower export that just doesn't include A2 -- not necessarily a
    # problem (could be outside the exported date range).
    narrower = _write_csv(tmp_path, [_row(code="A1")], filename="narrower.csv")
    report = reconcile(db, narrower, venue=hamilton)

    assert report.missing_from_export == ["A2"]
    assert report.is_clean is True


# --- Hardening regression tests (pre-deployment cycle) -----------------


def test_truncated_row_does_not_crash_and_still_flags_a_real_divergence(db, hamilton, tmp_path):
    """A row shorter than the header (missing trailing fields, which
    csv.DictReader fills with None rather than raising) must not crash
    the report -- including when its Code matches an already-imported
    booking and the row logic actually reaches the None fields. It
    should come through as a genuine divergence (empty status/attendee
    count no longer match what's on file), not be silently ignored."""
    original = _write_csv(tmp_path, [_row(code="A1"), _row(code="A2")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    path = tmp_path / "malformed.csv"
    # A1's row is truncated right after Code -- Status and everything
    # after it is missing, not just empty.
    path.write_text(HEADER + "A1\n" + _row(code="A2"), encoding="utf-8-sig")

    report = reconcile(db, str(path), venue=hamilton)  # must not raise

    assert report.matched_clean == 1  # A2 still compared successfully
    assert report.is_clean is False  # A1's missing status is a real, correctly-surfaced divergence
    assert any(d.code == "A1" for d in report.divergences)


def test_missing_column_entirely_does_not_crash_the_report(db, hamilton, tmp_path):
    """A CSV exported with a different/renamed column set (a future iVvy
    format change) must not crash the whole reconciliation run."""
    original = _write_csv(tmp_path, [_row(code="A1")], filename="original.csv")
    import_ivvy_csv(db, original, venue=hamilton)

    bad_header = "Code,Booking Name\n"
    bad_row = "A1,Some Event\n"
    path = tmp_path / "malformed_header.csv"
    path.write_text(bad_header + bad_row, encoding="utf-8-sig")

    report = reconcile(db, str(path), venue=hamilton)  # must not raise

    assert report.is_clean is False
