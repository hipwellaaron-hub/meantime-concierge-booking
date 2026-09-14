"""The digest reports what the checks found, not just two worklists.

Aaron, 2026-09-14: "If a check fires and I don't hear about it, we've built
a log, not a safeguard."

He is describing the overpayment guard shipped that morning. It catches the
money and then reports it to a banner on a booking page somebody has to
open, and to a Triage page that only runs when somebody presses a button.
build_digest gathered exactly two things -- wizard_eligible and
overdue_invoices -- so every reconciliation check and every flag reached
him only if he went looking.

GROUPED BY CHECK, and that is the design rather than a nicety. Listing
findings flat is what made Triage unreadable: 37 migration-era
CONFIRMED_WITHOUT_GATES rows buried four IMMINENT_NO_BEO bookings inside a
fortnight, and he nearly missed them. Grouped, the 37 collapse to one line
with a count and the urgent check keeps its own heading.

SMALLEST GROUPS FIRST, for the same reason: a check with three bookings is
nearly always the one that needs reading; a check with thirty is nearly
always a standing condition somebody already knows about.
"""
import datetime as dt
import uuid

from app.models import ReconciliationFinding
from app.services import digest as digest_service
from app.services.booking import change_status, flag_for_review
from app.models.booking import BookingStatus
from app.services.booking import create_booking

BASE = "https://book.meantime.com.au"


def _booking(db, loft, contact, name, days=20):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=days), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return b


def _finding(db, booking, check_code, detail="something to look at"):
    row = ReconciliationFinding(
        booking_id=booking.id, check_code=check_code, category="needs_human",
        detail=detail, first_seen_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def _render(db, hamilton):
    content = digest_service.build_digest(db, hamilton)
    subject, body = digest_service.render_digest_text(content, dashboard_base_url=BASE)
    return content, subject, body


# --- the two new sections ---------------------------------------------------


def test_an_open_finding_reaches_the_digest(db, hamilton, loft, contact):
    """THE one. A check that fires and is never mentioned is a log."""
    booking = _booking(db, loft, contact, "ZZDIGEST Overpaid")
    _finding(db, booking, "INVOICE_OVERPAID",
             "HAM-1001 has received $1950.00 against a total of $1450.00")

    content, subject, body = _render(db, hamilton)

    assert content.findings, "build_digest gathered no findings"
    assert "RECONCILIATION" in body
    assert "Invoice Overpaid" in body
    assert "ZZDIGEST Overpaid" in body
    assert "$1950.00" in body
    assert f"{BASE}/admin/bookings/{booking.id}" in body


def test_a_flag_reaches_the_digest(db, hamilton, loft, contact):
    """The overpayment guard raises one of these the moment money lands."""
    booking = _booking(db, loft, contact, "ZZDIGEST Flagged")
    change_status(db, booking, BookingStatus.tentative, actor="test")
    db.flush()
    flag_for_review(db, booking, note="OVERPAID: decide on a refund", actor="test")
    db.flush()

    content, subject, body = _render(db, hamilton)

    assert content.flagged_bookings, "build_digest gathered no flags"
    assert "FLAGGED, STILL OPEN" in body
    assert "ZZDIGEST Flagged" in body


def test_both_count_towards_the_subject_line(db, hamilton, loft, contact):
    """A subject saying "all clear" over a body listing an overpayment is
    worse than no email."""
    booking = _booking(db, loft, contact, "ZZDIGEST Counts")
    _finding(db, booking, "INVOICE_OVERPAID")

    content, subject, body = _render(db, hamilton)

    assert "all clear" not in subject
    assert "1 item" in subject
    assert content.is_empty is False


# --- the volume problem, which is the actual point --------------------------


def test_a_large_standing_group_does_not_bury_a_small_urgent_one(
    db, hamilton, loft, contact
):
    """Aaron's exact complaint about Triage, reproduced: many migration-era
    findings alongside a few imminent ones. The urgent group must still be
    listed in full and must appear FIRST."""
    for n in range(12):
        b = _booking(db, loft, contact, f"ZZBULK Migration {n}", days=100 + n)
        _finding(db, b, "CONFIRMED_WITHOUT_GATES", "Confirmed but deposit not paid")
    for n in range(3):
        b = _booking(db, loft, contact, f"ZZURGENT Imminent {n}", days=5 + n)
        _finding(db, b, "IMMINENT_NO_BEO", "Event inside the lead time with no Event Order")

    _, _, body = _render(db, hamilton)

    assert "Imminent No Beo (3)" in body
    assert "Confirmed Without Gates (12)" in body
    # All three urgent ones named.
    for n in range(3):
        assert f"ZZURGENT Imminent {n}" in body
    # The urgent heading comes first -- smallest group first.
    assert body.index("Imminent No Beo") < body.index("Confirmed Without Gates")


def test_the_bulk_group_is_summarised_not_silently_truncated(
    db, hamilton, loft, contact
):
    """A section that quietly shows five of twelve reads as "there are
    five". It has to say what it left out."""
    for n in range(12):
        b = _booking(db, loft, contact, f"ZZTRUNC Bulk {n}", days=100 + n)
        _finding(db, b, "CONFIRMED_WITHOUT_GATES", "Confirmed but deposit not paid")

    _, _, body = _render(db, hamilton)

    listed = sum(1 for n in range(12) if f"ZZTRUNC Bulk {n}" in body)
    assert listed == digest_service.FINDINGS_LISTED_PER_CHECK
    assert f"... and {12 - digest_service.FINDINGS_LISTED_PER_CHECK} more on Triage" in body


# --- and it must not become noise -------------------------------------------


def test_nothing_open_still_reads_as_all_clear(db, hamilton, loft, contact):
    """The digest already had this property and must keep it: a daily email
    that always has something in it stops being read."""
    content, subject, body = _render(db, hamilton)

    assert content.findings == []
    assert content.flagged_bookings == []
    assert "all clear" in subject
    assert "RECONCILIATION" not in body
    assert "FLAGGED" not in body
