"""Checking a client's wizard must not record them as having opened it.

The third instance of the same fault, and the only one with no existing
door to point at. Documents and invoices each had a staff preview route
sitting beside the client link, unused by the button that mattered. The
wizard had none, so the booking page's "Wizard link" went to /w/{token} --
the client's own URL -- and every staff click called
wizard_service.record_open.

WHY IT IS WORSE THAN THE OTHER TWO. opened_at is SET ONCE. Whoever loads
the link first consumes it, so a staff click did not merely mislabel the
open, it destroyed the record of the client's real one. And until now the
stamp wrote no BookingEvent and no actor at all, so there was nothing to
tell a staff click, a client, or a mail scanner prefetching the token out
of the resume email apart.

WHAT AARON GETS BACK. Aaron, 2026-09-14: "Null means nobody, and I'll rely
on that. A timestamp means someone and I won't." From this deploy forward
the timestamp also carries an event with an actor, so it becomes
attributable -- the staff door is closed, and what remains is the honest
ambiguity between a client and an automated fetch, which is why the actor
says "(auto)".
"""
import datetime as dt
import re

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services import wizard as wizard_service
from app.services.booking import create_booking

BOOKING_PAGE = "app/templates/admin/booking_detail.html"


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _booking_with_wizard(db, loft, contact, name="Wizard Open"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=25), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    session = wizard_service.get_or_create_session(db, booking, actor="staff:test@meantime.com.au")
    db.flush()
    return booking, session


# --- the staff door ---------------------------------------------------------


def test_the_staff_read_does_not_mark_the_wizard_opened(admin_client, db, hamilton, loft, contact):
    """THE one. opened_at is one-shot, so a staff click did not just
    mislabel the open -- it spent it."""
    booking, session = _booking_with_wizard(db, loft, contact, name="ZZWIZREAD Staff")
    assert session.opened_at is None

    page = admin_client.get(
        f"/admin/hamilton/bookings/{booking.id}/wizard/preview", follow_redirects=True
    )

    assert page.status_code == 200, page.text[:300]
    db.refresh(session)
    assert session.opened_at is None, "a staff read consumed the client's open stamp"


def test_the_staff_preview_says_what_it_is(admin_client, db, hamilton, loft, contact):
    """The page is otherwise indistinguishable from the client's own."""
    booking, _ = _booking_with_wizard(db, loft, contact, name="ZZWIZBANNER Staff")

    page = admin_client.get(
        f"/admin/hamilton/bookings/{booking.id}/wizard/preview", follow_redirects=True
    )

    assert "Staff preview of the client" in page.text
    assert "does not count as the client opening it" in page.text


def test_the_booking_page_sends_staff_to_the_preview(admin_client, db, hamilton, loft, contact):
    booking, session = _booking_with_wizard(db, loft, contact, name="ZZWIZLINK Staff")

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)

    assert f"/bookings/{booking.id}/wizard/preview" in page.text, (
        "the booking page does not offer the staff read"
    )
    assert f'href="/w/{session.access_token}"' not in page.text, (
        "the booking page still links staff at the client's own wizard URL"
    )


def test_no_booking_page_link_opens_the_client_wizard_url():
    """Structural, matching the /d/ and /i/ checks: the href is what broke,
    and Copy link is deliberately still the real client URL because that is
    the thing staff actually send."""
    import pathlib

    markup = pathlib.Path(BOOKING_PAGE).read_text(encoding="utf-8")
    hrefs = re.findall(r'href="(/w/\{\{[^"]*)"', markup)

    assert hrefs == [], f"a booking-page link opens the client's own wizard URL: {hrefs}"


# --- the client stamp, and its new audit row --------------------------------


def test_a_real_client_open_still_records(db, hamilton, loft, contact):
    """Don't remove the stamp -- the same rule as the document and invoice
    fixes. Null must keep meaning nobody."""
    booking, session = _booking_with_wizard(db, loft, contact, name="ZZWIZCLIENT Open")

    try:
        resp = _client(db).get(f"/w/{session.access_token}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text[:300]
    db.refresh(session)
    assert session.opened_at is not None, "the client's own open no longer records"


def test_the_open_now_leaves_an_audit_row(db, hamilton, loft, contact):
    """It wrote no event and no actor at all, which is why every existing
    opened_at is unattributable. From here it is auditable."""
    booking, session = _booking_with_wizard(db, loft, contact, name="ZZWIZEVENT Open")

    try:
        _client(db).get(f"/w/{session.access_token}")
    finally:
        app.dependency_overrides.clear()

    db.refresh(booking)
    opens = [e for e in booking.events if e.event_type == "wizard_opened"]
    assert len(opens) == 1, "the client's open left no audit row"
    assert opens[0].actor == "client (auto)"


def test_the_stamp_is_still_written_once(db, hamilton, loft, contact):
    """The set-once behaviour is deliberate and unchanged -- and it is the
    reason the staff door had to close rather than the stamp being made
    chattier. A second load must not add a second row either."""
    booking, session = _booking_with_wizard(db, loft, contact, name="ZZWIZONCE Open")

    try:
        client = _client(db)
        client.get(f"/w/{session.access_token}")
        db.refresh(session)
        first = session.opened_at
        client.get(f"/w/{session.access_token}")
    finally:
        app.dependency_overrides.clear()

    db.refresh(session)
    db.refresh(booking)
    assert session.opened_at == first
    assert len([e for e in booking.events if e.event_type == "wizard_opened"]) == 1
