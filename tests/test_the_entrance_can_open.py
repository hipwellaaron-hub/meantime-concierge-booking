"""The Entrance opens: one booking, end to end, through the real surfaces.

Every other two-venue test in this suite builds its fixtures by hand. That
proves the pieces and not the PATH -- and the path is what nobody has ever
walked, because the second venue does not exist yet. This walks it: create
the venue through the admin page a person would use, take a public enquiry,
run the wizard, generate the documents, issue the invoice, and check at
every step that nothing of Hamilton's has come along.

WHY IT IS ONE TEST FILE AND NOT SEVERAL. The failures worth finding here
are the ones that only appear in sequence -- a column nobody filled in at
step one printing blank at step six, a reference built from a prefix set
three steps earlier. A per-stage test with a hand-built fixture at the top
cannot see any of them, because the fixture supplies what the earlier
stage forgot.

NOTHING HERE TOUCHES PRODUCTION. Creating The Entrance for real is Aaron's
call and his point of no return; this is the rehearsal.
"""
import datetime as dt
import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import seed
from app.models import Space, Venue
from app.services import venue_readiness

# Nice Try Events Pty Ltd, the real entity (ABN recorded 2026-09-11). The
# bank details are DELIBERATELY fake: this file is tracked, and a real
# account number never goes into one.
ENTRANCE = {
    "trading_name": "Meantime The Entrance",
    "legal_name": "Nice Try Events Pty Ltd",
    "abn": "28 647 750 892",
    "address": "The Entrance NSW 2261",
    "phone": "02 4333 0000",
    "contact_name": "Aaron Hipwell",
    "contact_email": "entrance@example.com",
    "bank_account_name": "Nice Try Events Pty Ltd",
    "bank_bsb": "000-000",
    "bank_account_number": "00000000",
    "licence_number": "LIQO000000000",
    "licensed_manager": "Aaron Hipwell",
    "stripe_secret_key_env": "STRIPE_SECRET_KEY_ENTRANCE",
    "stripe_webhook_secret_env": "STRIPE_WEBHOOK_SECRET_ENTRANCE",
    "stripe_account_id": "acct_entrance_rehearsal",
}


# Columns the two venues may legitimately hold the same value in. "Only
# the operator is shared" (2026-09-11): Aaron runs both companies, so the
# contact and the licensed manager are the same person by design, and both
# venues trade the same days by his 2026-09-12 ruling. Everything else
# differing is the whole point of the design and is asserted below.
SHARED_BY_DESIGN = {"trading_days", "contact_name", "licensed_manager"}


def _csrf(client, url):
    page = client.get(url)
    assert page.status_code == 200, f"{url} -> {page.status_code}"
    return page.text, re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


@pytest.fixture()
def entrance(admin_client, db, hamilton):
    """The Entrance, created and filled in THROUGH THE ADMIN PAGE -- not by
    constructing rows.

    That is the point: the page is the only path a person has, so if it
    cannot produce a venue that the readiness check calls ready, the launch
    does not happen and this is where we find out.
    """
    venues_url = f"/admin/{hamilton.slug}/venues"

    _, token = _csrf(admin_client, venues_url)
    created = admin_client.post(
        venues_url,
        data={"csrf_token": token, "name": "The Entrance", "slug": "entrance"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text[:500]

    venue = db.scalar(select(Venue).where(Venue.slug == "entrance"))
    assert venue is not None

    _, token = _csrf(admin_client, venues_url)
    filled = admin_client.post(
        f"{venues_url}/{venue.id}",
        data={
            "csrf_token": token,
            "reference_prefix": "ENT",
            "trading_days_present": "1",
            **{f"day_{d}": "on" for d in (2, 3, 4, 5, 6)},
            **ENTRANCE,
        },
        follow_redirects=False,
    )
    assert filled.status_code == 303, filled.text[:500]
    db.expire_all()

    # A bookable room. The page deliberately does NOT invent one -- a room
    # is a business decision -- so this is the one step of set-up that is
    # still a database job, and saying so is half the value of this file.
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"),
        is_bookable=True, has_per_head_shortfall_fee=True,
    ))
    db.flush()
    return db.scalar(select(Venue).where(Venue.slug == "entrance"))


# --- stage one: the venue is set up ------------------------------------


def test_the_page_produces_a_venue_the_readiness_check_calls_ready(db, entrance):
    """THE first gate. If this fails, nothing below matters and the launch
    is blocked on something nobody has looked at."""
    readiness = venue_readiness.check(db, entrance)

    assert readiness.is_ready, f"still missing: {readiness.gaps}"


def test_it_has_its_own_identity_and_none_of_hamiltons(db, entrance, hamilton):
    """A different COMPANY, not a second branch. Read column by column
    against Hamilton's own row, so the test cannot go stale when either
    venue's details change."""
    for column in seed.CLIENT_FACING_COLUMNS:
        mine = getattr(entrance, column)
        theirs = getattr(hamilton, column)
        assert mine, f"{column} is empty on The Entrance"
        if column in SHARED_BY_DESIGN:
            continue
        assert mine != theirs, (
            f"{column} is identical to Hamilton's -- two companies sharing "
            "one legal or banking detail is the failure this whole design "
            "exists to prevent"
        )


def test_the_triage_space_came_with_it(db, entrance):
    """Created by the page, because a venue without it serves its enquiry
    form as a 200 and 500s on the submit."""
    spaces = db.scalars(select(Space).where(Space.venue_id == entrance.id)).all()

    assert seed.UNASSIGNED_SPACE_NAME in {s.name for s in spaces}
    assert any(s.is_bookable for s in spaces), "nothing to sell"


def test_hamilton_is_untouched(db, entrance, hamilton):
    """Creating a second company must not have moved anything of the
    first's. Cheap to assert, and the kind of thing nobody checks."""
    assert venue_readiness.check(db, hamilton).is_ready
    assert hamilton.reference_prefix == "HAM"


# --- stage two: a client enquires --------------------------------------


def _enquiry(**overrides):
    payload = dict(
        first_name="Robin", last_name="Vale", email="robin.vale@example.com",
        phone="0400333444", event_name="Vale 40th", event_date="2027-05-08",
        dates_flexible="false", event_type="Birthday", attendee_count=55,
        proposed_time_slot="Friday evening", comments="Hoping for the private bar.",
    )
    payload.update(overrides)
    return payload


@pytest.fixture()
def public_client(db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def enquiry(public_client, db, entrance):
    """A real POST to the real public route, the way a client reaches it."""
    from app.models import Booking

    form = public_client.get("/enquire/entrance")
    assert form.status_code == 200
    # The form a client actually sees names the company they are enquiring
    # with. A mis-linked slug serving Hamilton's form under The Entrance's
    # URL files every lead from that page at the wrong company.
    assert entrance.trading_name in form.text
    assert 'action="/enquire/entrance"' in form.text

    response = public_client.post("/enquire/entrance", data=_enquiry())
    # EXACTLY 303, not "in (200, 303)". A 200 is a rendered error page and a
    # 422 is a renamed field, and the looser version accepted both -- then
    # the lookup below found whatever booking an earlier fixture left
    # behind and every assertion in this file ran against the wrong row.
    assert response.status_code == 303, (
        f"the enquiry did not take: {response.status_code} {response.text[:300]}"
    )

    # SCOPED TO THE VENUE. Matching on event_name alone is the same mistake
    # in a test that the code is being checked for.
    booking = db.scalars(
        select(Booking).where(
            Booking.venue_id == entrance.id, Booking.event_name == "Vale 40th"
        )
    ).one()
    assert response.headers["location"] == f"/enquiries/{booking.id}/thanks"
    return booking


def test_the_enquiry_lands_on_the_entrance(db, enquiry, entrance, hamilton):
    """The one the triage space exists for -- and the one that decides
    whose books this booking is in."""
    assert enquiry.venue_id == entrance.id, "an Entrance enquiry landed on another venue"
    assert enquiry.venue_id != hamilton.id


def test_its_reference_carries_the_entrance_prefix(db, enquiry):
    """The string a client quotes back on the phone. Built from
    venues.reference_prefix, frozen at creation, and never rewritten -- so
    a wrong one is permanent."""
    assert enquiry.reference_code.startswith("ENT-"), enquiry.reference_code
    assert "HAM" not in enquiry.reference_code


def test_the_notification_would_go_out_as_the_entrance(db, enquiry, entrance, hamilton):
    """venue_mail refuses a venue missing any of the three identity fields,
    so this also proves the set-up page filled them. Asserted on the
    identity rather than by sending, because sending is not configured in
    tests and would prove nothing about which venue it named."""
    from app.services import notifications

    mail = notifications.venue_mail_for(enquiry)

    assert mail.trading_name == entrance.trading_name
    assert mail.contact_email == entrance.contact_email
    assert mail.trading_name != hamilton.trading_name
    assert mail.contact_email != hamilton.contact_email


def test_the_notification_body_names_no_other_company(db, enquiry, hamilton):
    from app.services import notifications

    body = notifications.build_enquiry_notification_body(enquiry)

    assert enquiry.reference_code in body
    for leak in (hamilton.trading_name, hamilton.abn, hamilton.bank_account_number):
        if leak:
            assert leak not in body, f"the enquiry email carries Hamilton's {leak!r}"


def test_the_thanks_page_renders(public_client, db, enquiry):
    response = public_client.get(f"/enquiries/{enquiry.id}/thanks")

    assert response.status_code == 200


# --- stage three: the documents ----------------------------------------


@pytest.fixture()
def booked(db, enquiry, entrance):
    """Triaged into the real room, the way staff move an enquiry on."""
    from app.services.booking import assign_space_and_time

    room = db.scalars(
        select(Space).where(Space.venue_id == entrance.id, Space.is_bookable.is_(True))
    ).one()
    assign_space_and_time(
        db, enquiry, space_id=room.id,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), actor="staff:test",
    )
    db.flush()
    return enquiry


def _hamilton_identity(hamilton):
    """Every string of Hamilton's that must never appear on another
    company's document. Read off the row, so the check cannot go stale
    when Hamilton's own details change."""
    values = []
    for column in seed.CLIENT_FACING_COLUMNS:
        if column in SHARED_BY_DESIGN:
            # Aaron's own name appears on both companies' documents,
            # correctly. Searching for it would flag every one of them.
            continue
        value = getattr(hamilton, column)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def test_the_agreement_carries_nice_try_events_and_not_meantime(db, booked, entrance, hamilton):
    """THE one this whole architecture exists for. A contract naming the
    wrong company, or the wrong bank account, is not a bug -- it is a
    client paying the wrong entity."""
    from app.models.document import DocumentType
    from app.services import documents as documents_service
    from app.services.document_generation import generate_agreement_content

    document = documents_service.create_new_version(
        db, booked, DocumentType.agreement, generate_agreement_content(booked), actor="staff:test"
    )

    blob = repr(document.content)
    assert entrance.legal_name in blob, "the agreement does not name the contracting company"
    assert entrance.abn in blob, "the agreement does not carry its own ABN"

    for leak in _hamilton_identity(hamilton):
        assert leak not in blob, (
            f"The Entrance's agreement carries Hamilton's {leak!r}"
        )


def test_the_event_order_carries_its_own_venue(db, booked, entrance, hamilton):
    from app.models.document import DocumentType
    from app.services import documents as documents_service
    from app.services.document_generation import generate_beo_content

    document = documents_service.create_new_version(
        db, booked, DocumentType.beo, generate_beo_content(booked), actor="staff:test"
    )

    blob = repr(document.content)
    for leak in _hamilton_identity(hamilton):
        assert leak not in blob, f"The Entrance's Event Order carries Hamilton's {leak!r}"


def test_the_rendered_client_page_names_the_right_company(public_client, db, booked, entrance, hamilton):
    """The document CONTENT is one thing; what the client actually opens is
    another -- the page renders identity live for an unpaid document, so a
    clean content dict proves nothing about the screen."""
    from app.models.document import DocumentType
    from app.services import documents as documents_service
    from app.services.document_generation import generate_agreement_content

    document = documents_service.create_new_version(
        db, booked, DocumentType.agreement, generate_agreement_content(booked), actor="staff:test"
    )
    documents_service.mark_sent(db, document, actor="staff:test")
    db.flush()

    page = public_client.get(f"/d/{document.access_token}")
    assert page.status_code == 200, page.status_code

    assert entrance.trading_name in page.text
    for leak in _hamilton_identity(hamilton):
        assert leak not in page.text, (
            f"the client's own agreement page shows Hamilton's {leak!r}"
        )


# --- stage four: the money ---------------------------------------------


@pytest.fixture()
def deposit(db, booked):
    from app.services import invoicing

    invoice = invoicing.create_deposit_invoice(
        db, booked, due_date=dt.date(2027, 4, 10), actor="staff:test"
    )
    db.flush()
    return invoice


def test_the_invoice_takes_its_own_register(db, deposit, entrance, hamilton):
    """Each venue numbers its own invoices, and the reference is what the
    client quotes when they pay. Assigned by a database trigger, frozen,
    and never rewritten."""
    assert deposit.venue_id == entrance.id
    assert deposit.invoice_reference.startswith("ENT-"), deposit.invoice_reference
    assert "HAM" not in deposit.invoice_reference


def test_the_first_entrance_invoice_starts_its_own_count(db, deposit, hamilton):
    """A separate counter, not a continuation of Hamilton's. A client
    receiving ENT-1047 on the venue's first week would be reading the other
    company's history."""
    from app.models import Invoice

    hamilton_numbers = db.scalars(
        select(Invoice.invoice_number).where(Invoice.venue_id == hamilton.id)
    ).all()
    if hamilton_numbers:
        assert deposit.invoice_number <= min(hamilton_numbers) or deposit.invoice_number < max(hamilton_numbers), (
            "The Entrance's first invoice continued Hamilton's numbering"
        )


def test_the_client_invoice_page_shows_nice_try_events_bank_details(
    public_client, db, deposit, entrance, hamilton
):
    """THE money one. The bank block is read LIVE on an unpaid invoice, so
    this is the screen that decides which company's account a client pays
    into."""
    from app.services import invoicing

    invoicing.mark_sent(db, deposit, actor="staff:test")
    db.flush()

    page = public_client.get(f"/i/{deposit.access_token}")
    assert page.status_code == 200, page.status_code

    assert entrance.bank_bsb in page.text, "the invoice does not show its own BSB"
    assert entrance.bank_account_number in page.text
    assert entrance.legal_name in page.text

    for leak in _hamilton_identity(hamilton):
        assert leak not in page.text, (
            f"The Entrance's invoice tells the client to pay Hamilton's {leak!r}"
        )


def test_a_paid_invoice_freezes_the_account_it_was_paid_into(db, deposit, entrance):
    """A receipt reproduces. Once paid, the account is frozen onto the
    invoice so a later change to the venue row cannot rewrite what the
    client already paid."""
    from app.services import invoicing

    invoicing.mark_sent(db, deposit, actor="staff:test")
    invoicing.record_payment(
        db, deposit, amount=deposit.total, method="bank_transfer", actor="staff:test",
    )
    db.flush()

    frozen = deposit.paid_to_account or {}
    assert frozen, "nothing was frozen at payment"
    # The snapshot's own key names, not the column names -- it is a record
    # of an account, not a copy of a row.
    assert frozen.get("account_number") == entrance.bank_account_number
    assert frozen.get("bsb") == entrance.bank_bsb
    assert frozen.get("legal_name") == entrance.legal_name
    assert frozen.get("frozen_at"), "the snapshot does not say when it was taken"


# --- stage five: what Aaron sees the next morning ----------------------


def test_the_nightly_run_covers_both_venues(db, booked, entrance, hamilton):
    """Jobs loop venues. A second company whose reconciliation never runs
    has an empty Triage, and an empty Triage looks exactly like a clean
    one."""
    from app.services import reconciliation

    hamilton_result = reconciliation.run(db, hamilton)
    entrance_result = reconciliation.run(db, entrance)

    assert entrance_result.total_open >= 0 and hamilton_result.total_open >= 0

    # And one venue's run must not close the other's findings.
    open_after = {f.booking_id for f in reconciliation.open_findings(db, entrance)}
    reconciliation.run(db, hamilton)
    assert {f.booking_id for f in reconciliation.open_findings(db, entrance)} == open_after, (
        "running Hamilton's reconciliation resolved The Entrance's findings"
    )


def test_the_digest_arrives_as_one_email_with_a_section_each(db, booked, entrance, hamilton):
    """Aaron, 2026-09-12: one email with the venues sectioned, because two
    emails means one gets skimmed. This is the first time there has ever
    been a second section to render."""
    from app.services import digest

    per_venue = [
        (hamilton, digest.build_digest(db, hamilton)),
        (entrance, digest.build_digest(db, entrance)),
    ]
    subject, body = digest.render_combined_digest(
        per_venue, dashboard_base_url="https://book.meantime.com.au"
    )

    assert hamilton.trading_name.upper() in body
    assert entrance.trading_name.upper() in body
    assert body.index(hamilton.trading_name.upper()) != body.index(entrance.trading_name.upper())
    # The subject counts across both, not per venue.
    total = sum(content.item_count for _, content in per_venue)
    if total:
        assert str(total) in subject


def test_a_brand_new_venue_does_not_flood_the_digest(db, booked, entrance):
    """A venue that opened yesterday with one enquiry on the books should
    not arrive as a page of findings. If this fails, the first morning
    after launch is unreadable and Aaron stops reading it -- which is the
    failure mode every check in this system is written against."""
    from app.services import digest, reconciliation

    reconciliation.run(db, entrance)
    content = digest.build_digest(db, entrance)

    assert content.venue_gaps == [], f"set-up is incomplete: {content.venue_gaps}"
    assert content.failed_sections == []
    assert content.item_count <= 3, (
        f"a one-booking venue produced {content.item_count} items on its first "
        f"morning: findings={[f.check_code for f in content.findings]}"
    )


def test_healthz_still_reads_the_same_with_two_venues(db, entrance):
    """Every check on that page folds across venues. A second company that
    is fully set up must not move it."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        body = TestClient(app).get("/healthz").json()
    finally:
        app.dependency_overrides.clear()

    assert body["checks"]["venues_ready"] is True, (
        "adding The Entrance made /healthz report a venue that is not ready"
    )
    assert body["checks"]["venues_present"] is True


# --- the same client, both venues --------------------------------------


def test_the_same_client_can_enquire_at_both_venues_the_same_day(
    public_client, db, entrance, hamilton
):
    """THE cross-venue one, and it is not contrived: a client deciding
    between the two rooms opens both forms and submits the same event name
    and date within a minute.

    Contacts are shared by design, so both enquiries resolve to one Contact
    -- and the duplicate guard used to match on (contact, event name, date)
    with NO venue clause. The second venue got no booking at all: the guard
    handed back the first venue's row, is_new came back False, and the
    route gates the notification and both background tasks on is_new. A
    lead lost silently, filed at the other company, with a thanks page
    naming an event they never booked there.
    """
    from app.models import Booking

    payload = _enquiry(event_name="Vale 40th Decider", email="decider@example.com")

    first = public_client.post("/enquire/hamilton", data=payload)
    second = public_client.post("/enquire/entrance", data=payload)

    assert first.status_code == 303 and second.status_code == 303

    bookings = db.scalars(
        select(Booking).where(Booking.event_name == "Vale 40th Decider")
    ).all()
    venues = {b.venue_id for b in bookings}

    assert len(bookings) == 2, (
        f"the same client enquiring at both venues produced {len(bookings)} "
        "booking(s) -- one company never heard about the lead"
    )
    assert venues == {hamilton.id, entrance.id}
    # And each thanks page names its own booking, not the other's.
    entrance_booking = next(b for b in bookings if b.venue_id == entrance.id)
    assert second.headers["location"] == f"/enquiries/{entrance_booking.id}/thanks"


def test_one_submission_id_posted_to_both_venues_still_makes_two_bookings(
    public_client, db, entrance, hamilton
):
    """Worse than the fifteen-second window, because submission_id ignores
    the clock entirely: a page restored from bfcache and re-submitted on
    the other company's form used to hand back the first venue's booking
    forever."""
    from app.models import Booking

    shared = str(uuid.uuid4())
    payload = _enquiry(
        event_name="Vale 40th Bfcache", email="bfcache@example.com", submission_id=shared,
    )

    public_client.post("/enquire/hamilton", data=payload)
    public_client.post("/enquire/entrance", data=payload)

    bookings = db.scalars(
        select(Booking).where(Booking.event_name == "Vale 40th Bfcache")
    ).all()

    assert {b.venue_id for b in bookings} == {hamilton.id, entrance.id}


def test_a_genuine_repeat_at_one_venue_is_still_deduplicated(public_client, db, entrance):
    """The control, and it earns its place: adding a venue predicate must
    not turn the duplicate guard off. A double-clicked submit is still one
    enquiry."""
    from app.models import Booking

    payload = _enquiry(event_name="Vale 40th Twice", email="twice@example.com")

    public_client.post("/enquire/entrance", data=payload)
    public_client.post("/enquire/entrance", data=payload)

    bookings = db.scalars(
        select(Booking).where(Booking.event_name == "Vale 40th Twice")
    ).all()

    assert len(bookings) == 1, "a double-clicked submit made two enquiries"
