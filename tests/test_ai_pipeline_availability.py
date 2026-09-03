"""Pipeline and availability (brief sections 3.0, 3.2) and the acceptance
cross-check (section 12).

The cross-check is the point of this file. `/availability` and
`/bookings?date=` are computed by deliberately separate query paths, so
comparing them across ninety days is a real test rather than a tautology.
If someone later makes one call the other, this test keeps passing while
silently proving nothing -- so it also asserts the two paths stay distinct.
"""

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import BookingEvent, Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.models.invoice import InvoiceType
from app.models.payment import PaymentMethod
from app.models.wizard_session import WizardSession, WizardSessionStatus
from app.services import ai_availability, ai_pipeline
from app.services import documents as documents_service
from app.services import invoicing
from app.services.booking import change_status, create_booking
from app.services.document_generation import generate_agreement_content

TOKEN = "test-ai-token-do-not-use-in-production"
FUTURE = dt.date.today() + dt.timedelta(days=45)


@pytest.fixture()
def ai_client(db, hamilton, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_token", TOKEN)
    monkeypatch.setattr(settings, "ai_access_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client
    finally:
        app.dependency_overrides.clear()


def _booking(db, space, *, name, when=FUTURE, start=dt.time(18, 0), end=dt.time(23, 0)):
    contact = Contact(name=f"{name} Client", email=f"{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=when,
        start_time=start, end_time=end, event_name=name, event_type="birthday",
        adult_count=50, child_count=0, notes=None, actor="staff:test",
    )


def _send_agreement(db, booking):
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="staff:test"
    )
    return documents_service.mark_sent(db, doc, actor="staff:test")


def _send_deposit(db, booking):
    inv = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": "500.00"}],
        dt.date.today(), actor="staff:test",
    )
    return invoicing.mark_sent(db, inv, actor="staff:test")


def _stage_of(db, booking):
    db.refresh(booking)
    return ai_pipeline.compute_stage(booking)


# --- stage, one booking at each computable stage ------------------------


def test_stage_enquiry(db, loft):
    assert _stage_of(db, _booking(db, loft, name="Enquiry Only")) == "enquiry"


def test_stage_offered_once_both_are_sent(db, loft):
    b = _booking(db, loft, name="Offered")
    _send_agreement(db, b)
    _send_deposit(db, b)
    assert _stage_of(db, b) == "offered"


def test_stage_signed_unpaid_is_the_laura_solway_case(db, loft):
    """Agreement signed, no payment. This must not read as confirmed."""
    b = _booking(db, loft, name="Signed Unpaid")
    doc = _send_agreement(db, b)
    _send_deposit(db, b)
    documents_service.sign(db, doc, signer_name="Laura", signer_ip="1.2.3.4")
    assert _stage_of(db, b) == "signed_unpaid"


def test_stage_paid_unsigned(db, loft):
    b = _booking(db, loft, name="Paid Unsigned")
    inv = _send_deposit(db, b)
    invoicing.record_payment(
        db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="staff:test"
    )
    assert _stage_of(db, b) == "paid_unsigned"


def test_stage_confirmed_means_both_and_no_wizard_yet(db, loft):
    b = _booking(db, loft, name="Confirmed Both")
    doc = _send_agreement(db, b)
    inv = _send_deposit(db, b)
    documents_service.sign(db, doc, signer_name="Client", signer_ip="1.2.3.4")
    invoicing.record_payment(
        db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="staff:test"
    )
    assert _stage_of(db, b) == "confirmed"


def test_stage_wizard_supersedes_confirmed(db, loft):
    b = _booking(db, loft, name="Wizard Sent")
    doc = _send_agreement(db, b)
    inv = _send_deposit(db, b)
    documents_service.sign(db, doc, signer_name="Client", signer_ip="1.2.3.4")
    invoicing.record_payment(
        db, inv, amount=Decimal("500.00"), method=PaymentMethod.card, actor="staff:test"
    )
    db.add(
        WizardSession(
            booking_id=b.id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=21),
        )
    )
    db.commit()
    assert _stage_of(db, b) == "wizard_sent"


def test_stage_wizard_submitted(db, loft):
    b = _booking(db, loft, name="Wizard Submitted")
    session = WizardSession(
        booking_id=b.id,
        status=WizardSessionStatus.submitted,
        submitted_at=dt.datetime.now(dt.timezone.utc),
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=21),
    )
    db.add(session)
    db.commit()
    assert _stage_of(db, b) == "wizard_submitted"


def test_stage_beo_sent(db, loft):
    b = _booking(db, loft, name="Beo Sent")
    beo = documents_service.create_new_version(
        db, b, DocumentType.beo, {"x": 1}, actor="staff:test"
    )
    documents_service.mark_sent(db, beo, actor="staff:test")
    assert _stage_of(db, b) == "beo_sent"


def test_stage_finalised_rests_on_the_final_invoice(db, loft):
    b = _booking(db, loft, name="Finalised")
    final = invoicing.create_final_invoice(
        db, b, line_items=[{"description": "Food", "quantity": 1, "unit_price": "1000.00"}],
        due_date=b.event_date, actor="staff:test",
    )
    invoicing.mark_sent(db, final, actor="staff:test")
    invoicing.record_payment(
        db, final, amount=final.total, method=PaymentMethod.bank_transfer, actor="staff:test"
    )
    assert _stage_of(db, b) == "finalised"


def test_stage_archived_covers_terminal_and_past(db, loft):
    cancelled = _booking(db, loft, name="Cancelled One")
    change_status(db, cancelled, BookingStatus.cancelled, actor="staff:test")
    assert _stage_of(db, cancelled) == "archived"

    past = _booking(db, loft, name="Past One", when=dt.date.today() - dt.timedelta(days=3))
    assert _stage_of(db, past) == "archived"


# --- the two honest limits ----------------------------------------------


def test_replied_never_fires_until_a_reply_is_logged(db, loft):
    """Staff reply in Gmail, which Concierge never sees. The stage is not
    approximated from status -- it stays `enquiry` until the event exists."""
    b = _booking(db, loft, name="Answered By Email")
    assert _stage_of(db, b) == "enquiry"

    db.add(
        BookingEvent(
            booking_id=b.id,
            event_type=ai_pipeline.REPLY_LOGGED_EVENT,
            actor="staff:test",
        )
    )
    db.commit()
    assert _stage_of(db, b) == "replied"


def test_awaiting_flips_to_staff_when_the_client_acts(db, loft):
    b = _booking(db, loft, name="Awaiting Flip")
    awaiting, _, _ = ai_pipeline.compute_awaiting(b)
    assert awaiting == "client"  # staff created it, ball is with the client

    doc = _send_agreement(db, b)
    documents_service.sign(db, doc, signer_name="Client Person", signer_ip="1.2.3.4")
    db.refresh(b)
    awaiting, _, last_by = ai_pipeline.compute_awaiting(b)
    assert awaiting == "staff"
    assert last_by.lower().startswith("client")


# --- availability -------------------------------------------------------


def test_open_enquiries_are_listed_and_contested_agrees(ai_client, db, loft):
    """'Free' and 'nobody else is asking' are different facts."""
    held = _booking(db, loft, name="First Interest")
    change_status(db, held, BookingStatus.tentative, actor="staff:test")
    _booking(db, loft, name="Second Interest")
    _booking(db, loft, name="Third Interest")

    resp = ai_client.get(f"/api/ai/availability?date={FUTURE.isoformat()}")
    assert resp.status_code == 200
    block = next(
        s for s in resp.json()["days"][0]["spaces"] if s["space"] == loft.name
    )
    assert len(block["tentative"]) == 1
    assert len(block["open_enquiries"]) == 2
    assert block["contested"] is True
    assert {e["contact_name"] for e in block["open_enquiries"]}

    pipeline = ai_client.get("/api/ai/pipeline").json()
    contested = [r for r in pipeline["records"] if r["contested"]]
    assert len(contested) >= 3, "every record on the contested slot must say so"


def test_lunch_and_evening_in_one_room_do_not_conflict(ai_client, db, mezzanine):
    """Renee Davies (lunch) and Alyssa Ross (evening), Mezzanine, same day."""
    when = dt.date(2026, 11, 28)
    _booking(db, mezzanine, name="Renee Lunch", when=when,
             start=dt.time(11, 30), end=dt.time(15, 0))
    _booking(db, mezzanine, name="Alyssa Evening", when=when,
             start=dt.time(18, 0), end=dt.time(23, 30))

    resp = ai_client.get(f"/api/ai/availability?date={when.isoformat()}")
    block = next(s for s in resp.json()["days"][0]["spaces"] if s["space"] == mezzanine.name)
    assert len(block["open_enquiries"]) == 2, "both must be visible"

    records = ai_client.get("/api/ai/pipeline").json()["records"]
    for ref in [r for r in records if r["space"] == mezzanine.name]:
        assert ref["contested"] is False, "non-overlapping times must not contest"


def test_day_of_week_is_correct_across_a_year_and_a_leap_boundary(ai_client):
    checks = {
        "2026-11-21": "Saturday",
        "2026-11-20": "Friday",
        "2027-01-01": "Friday",
        "2028-02-28": "Monday",
        "2028-02-29": "Tuesday",  # leap day
        "2028-03-01": "Wednesday",
    }
    for date_str, expected in checks.items():
        resp = ai_client.get(f"/api/ai/availability?date={date_str}")
        assert resp.json()["days"][0]["day_of_week"] == expected, date_str


def test_every_response_carries_as_of(ai_client):
    for path in ["/api/ai/pipeline", f"/api/ai/availability?date={FUTURE.isoformat()}",
                 f"/api/ai/bookings?date={FUTURE.isoformat()}"]:
        body = ai_client.get(path).json()
        assert body.get("as_of"), path
        dt.datetime.fromisoformat(body["as_of"])  # parseable


# --- section 12 acceptance: the ninety-day cross-check ------------------


def test_availability_and_bookings_by_date_agree_across_90_days(ai_client, db, loft, mezzanine, lounge):
    """The two endpoints are computed by separate query paths. Across
    ninety days, for every space, they must name exactly the same
    occupants -- otherwise the AI could be told a slot is free by one and
    taken by the other, which is precisely the failure this guards."""
    base = dt.date.today()
    # A spread that exercises every bucket: enquiry, tentative, confirmed,
    # a two-in-one-room day, and days with nothing at all.
    seeded = [
        (loft, 3, dt.time(18, 0), dt.time(23, 0), BookingStatus.enquiry),
        (loft, 3, dt.time(11, 0), dt.time(14, 0), BookingStatus.tentative),
        (mezzanine, 10, dt.time(18, 0), dt.time(23, 0), BookingStatus.confirmed),
        (lounge, 21, dt.time(12, 0), dt.time(16, 0), BookingStatus.enquiry),
        (loft, 44, dt.time(19, 0), dt.time(23, 30), BookingStatus.tentative),
        (mezzanine, 67, dt.time(11, 30), dt.time(15, 0), BookingStatus.enquiry),
        (mezzanine, 67, dt.time(18, 0), dt.time(23, 30), BookingStatus.confirmed),
        (loft, 89, dt.time(18, 0), dt.time(23, 0), BookingStatus.confirmed),
    ]
    for i, (space, offset, start, end, status) in enumerate(seeded):
        b = _booking(db, space, name=f"Seed {i}", when=base + dt.timedelta(days=offset),
                     start=start, end=end)
        if status != BookingStatus.enquiry:
            change_status(db, b, status, actor="staff:test")

    last = base + dt.timedelta(days=89)
    avail = ai_client.get(
        f"/api/ai/availability?from={base.isoformat()}&to={last.isoformat()}"
    ).json()
    assert len(avail["days"]) == 90

    mismatches = []
    for day in avail["days"]:
        date_str = day["date"]
        from_availability = {
            entry["reference"]
            for block in day["spaces"]
            for bucket in ("confirmed", "tentative", "open_enquiries")
            for entry in block[bucket]
        }
        from_bookings = {
            b["reference"]
            for b in ai_client.get(f"/api/ai/bookings?date={date_str}").json()["bookings"]
        }
        if from_availability != from_bookings:
            mismatches.append((date_str, sorted(from_availability), sorted(from_bookings)))

    assert not mismatches, f"availability and bookings-by-date disagree: {mismatches[:5]}"


def test_cross_check_also_agrees_per_space(ai_client, db, loft, mezzanine):
    when = dt.date.today() + dt.timedelta(days=30)
    _booking(db, loft, name="Loft Party", when=when)
    _booking(db, mezzanine, name="Mezz Party", when=when)

    avail = ai_client.get(f"/api/ai/availability?date={when.isoformat()}").json()
    for block in avail["days"][0]["spaces"]:
        expected = {
            e["reference"]
            for bucket in ("confirmed", "tentative", "open_enquiries")
            for e in block[bucket]
        }
        got = {
            b["reference"]
            for b in ai_client.get(
                f"/api/ai/bookings?date={when.isoformat()}&space={block['space']}"
            ).json()["bookings"]
        }
        assert expected == got, block["space"]


def test_the_two_paths_are_actually_independent():
    """Guards the guard. If someone makes one function delegate to the
    other, the ninety-day test above would still pass while proving
    nothing, so assert the separation directly -- by inspecting calls in
    the parsed source, not by substring-matching the prose around it."""
    import ast
    import inspect

    module = ast.parse(inspect.getsource(ai_availability))
    functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }

    def called_names(func_name):
        names = set()
        for node in ast.walk(functions[func_name]):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        return names

    assert "bookings_on_date" not in called_names("build_availability")
    assert "build_availability" not in called_names("bookings_on_date")
    assert "load_touching" not in called_names("bookings_on_date")
