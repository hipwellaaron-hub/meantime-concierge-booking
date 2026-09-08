"""The wizard submission is a regenerate with nobody to ask.

Staff who press Regenerate are shown what a rebuild would destroy and
decide field by field. A client submitting the wizard reaches the same
create_new_version, from a request with no staff member in it, and until
now it replaced the whole document -- an approved allergy note included --
without a word. The guard existed; this was the door it was not on.

There is nobody to ask mid-request, so the answer is not to decide: keep
what a person wrote, and put every carried value in front of staff through
outstanding_items, which is the list the submission email leads with.

The load-bearing test here goes through the client's real HTTP route
rather than the service function, because that is the path a client
actually takes and it is where Aaron asked for the proof.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import content_authorship as ca
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking
from app.services.wizard import BarStructure, CakeChoiceType, MusicType
from app.services.document_generation import NO_DIETARIES, generate_beo_content

ALLERGY = "1x severe nut allergy (table 4). Kitchen briefed."


@pytest.fixture()
def client(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _make_booking(db, space, *, event_date):
    contact = Contact(name="Carry Forward Contact", email=f"carry.{event_date}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=event_date,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Carry Forward Booking",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )



# Copied from tests/test_wizard_generation.py rather than written from
# memory: the standing check is that a test's inputs match what the real
# caller passes, and my first version invented keyword names.
def _complete_all_steps(db, session, menu_items, *, accessibility_needs=None):
    grazing = menu_items["Grazing Platter"]
    basics_booking = session.booking
    wizard_service.save_basics_step(
        db, session, start_time=basics_booking.start_time, end_time=basics_booking.end_time,
        food_service_time=dt.time(18, 30), setup_access_time=dt.time(14, 0),
        adult_count=basics_booking.adult_count, child_count=0, actor="client",
    )
    wizard_service.save_food_step(
        db, session, platters=[{"menu_item_id": grazing.id, "quantity": 2}], pizzas=[], actor="client"
    )
    wizard_service.save_beverage_step(
        db, session, bar_structure=BarStructure.cash_bar, bar_limit=None, bar_inclusions=None, actor="client"
    )
    wizard_service.save_music_step(
        db, session, music_type=MusicType.own_playlist, notes="Chill playlist", bump_in_notes=None, actor="client"
    )
    # An empty vendors list is a real "no vendors" answer -- the step is
    # complete without any vendor rows.
    wizard_service.save_vendors_step(db, session, vendors=[], actor="client")
    wizard_service.save_extras_step(
        db, session, cake_choice_type=CakeChoiceType.none, cake_menu_item_id=None, cake_notes=None,
        decorations_notes=None, layout_notes="No special layout", dietary_requirements=None,
        accessibility_needs=accessibility_needs, additional_notes=None, actor="client",
    )
    # The AV step only exists for Loft bookings; a completeness check that
    # flagged a skipped AV step on any other space would be flagging a
    # step the client never saw.
    if session.booking.space.name == "The Loft":
        wizard_service.save_av_step(
            db, session, video_slideshow=False, microphones_for_speeches=False, notes=None, actor="client"
        )


def _booking_with_a_typed_allergy(db, loft, menu_items):
    """A confirmed booking whose current Event Order carries an allergy note
    a person typed and the record names as theirs."""
    booking = _make_booking(db, loft, event_date=dt.date(2027, 3, 6))
    change_status(db, booking, BookingStatus.confirmed, actor="test")

    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    documents_service.update_content(
        db,
        document,
        {**document.content, "dietaries": ALLERGY},
        actor="staff:aaron@meantime.com.au",
        authored_fields=dr.PROTECTED_FIELD_NAMES,
        placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)
    assert ca.authored(document.content) == {"dietaries"}
    return booking


def test_a_client_submission_does_not_destroy_a_typed_allergy(db, loft, menu_items, client):
    """END TO END, through the client's own HTTP route. Before this the
    submission replaced the Event Order outright and the allergy became
    "No dietary requirements declared" on version 2, with nothing said to
    anybody -- Aaron's original incident, reached by the one path that had
    no confirmation screen in front of it."""
    booking = _booking_with_a_typed_allergy(db, loft, menu_items)
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 200, response.text
    body = response.json()

    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.version == 2, "the submission still produced a new version"
    assert current.content["dietaries"] == ALLERGY, "and it still carries the person's words"
    assert current.content["dietaries"] != NO_DIETARIES

    assert any("Dietaries" in item for item in body["outstanding_items"]), body["outstanding_items"]
    assert body["is_clean"] is False, "a carried value is something staff must look at"


def test_the_carried_value_keeps_its_authorship(db, loft, menu_items, client):
    """Otherwise the very next regenerate sees a document with no record,
    treats the allergy as the generator's, and destroys it -- the guard
    holding for exactly one round."""
    booking = _booking_with_a_typed_allergy(db, loft, menu_items)
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    client.post(f"/w/{session.access_token}/review", json={})

    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert ca.authored(current.content) == {"dietaries"}


def test_a_field_the_wizard_rebuilds_is_not_recorded_as_a_persons(db, loft, menu_items, client):
    """The other half: the record must not grow to cover values the wizard
    just produced. Music comes from the client's own answer, and nobody
    typed it into the document."""
    booking = _booking_with_a_typed_allergy(db, loft, menu_items)
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    client.post(f"/w/{session.access_token}/review", json={})

    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert "music" not in ca.authored(current.content)


def test_a_submission_with_nothing_at_risk_says_nothing_extra(db, loft, menu_items, client):
    """The ordinary case has to stay ordinary. A booking whose Event Order
    nobody has written into must not gain a review item, or the list staff
    read stops meaning anything."""
    booking = _make_booking(db, loft, event_date=dt.date(2027, 4, 10))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    response = client.post(f"/w/{session.access_token}/review", json={})

    assert response.status_code == 200, response.text
    carried = [item for item in response.json()["outstanding_items"] if "kept from the previous" in item]
    assert carried == []


def test_a_replaced_value_loses_its_record(db, loft, menu_items, client):
    """The other direction. A recorded field the guard does NOT protect --
    here because its stored value is one of the generator's placeholders --
    is rebuilt by the wizard, so the record must not keep claiming a person
    wrote it. Otherwise the next regenerate preserves the wizard's own text
    as somebody's words."""
    booking = _make_booking(db, loft, event_date=dt.date(2027, 5, 15))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    # recorded, but holding a placeholder: nothing of anybody's to protect
    document.content = ca.record(
        {**document.content, "bar_structure": "[REVIEW] add bar structure"}, ["bar_structure"]
    )
    db.commit()
    db.refresh(document)
    assert "bar_structure" in ca.authored(document.content)

    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)
    client.post(f"/w/{session.access_token}/review", json={})

    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert "bar_structure" not in ca.authored(current.content), (
        "the wizard's value is the wizard's, whatever the old record said"
    )


def test_a_value_the_wizard_rebuilds_identically_keeps_its_record(db, loft, menu_items, client):
    """The case "authored minus kept" got wrong, and the case that showed
    the record was not being carried at all.

    internal_notes is the field to use: wizard_generation reads the prior
    Event Order's internal_notes and puts it straight back, so the wizard
    reproduces it exactly. Nothing is at risk, so it is not in `kept` --
    but the person's words are still what the document says, and dropping
    the record hands the field to the next regenerate as the generator's."""
    typed = "Kitchen: plate the nut-free platter separately."
    booking = _make_booking(db, loft, event_date=dt.date(2027, 6, 12))
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    documents_service.update_content(
        db, document, {**document.content, "internal_notes": typed},
        actor="staff:aaron@meantime.com.au",
        authored_fields=dr.PROTECTED_FIELD_NAMES, placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)
    assert ca.authored(document.content) == {"internal_notes"}

    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)
    client.post(f"/w/{session.access_token}/review", json={})

    current = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert current.content["internal_notes"] == typed, "the wizard reproduces it verbatim"
    assert ca.authored(current.content) == {"internal_notes"}, (
        "unchanged text is still their text, so the record has to survive the rebuild"
    )


def test_the_current_document_is_locked_before_it_is_read(db, loft, menu_items, client):
    """This reads the current document, decides from it, and then writes --
    the same shape that let an approval landing in between be destroyed on
    the staff path (proved live 2026-09-06). A single-threaded test cannot
    show the race, so it asserts the lock was actually taken."""
    from sqlalchemy import event as sa_event

    booking = _booking_with_a_typed_allergy(db, loft, menu_items)
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)

    locked = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        collapsed = " ".join(statement.split()).upper()
        if "FOR UPDATE" in collapsed and "DOCUMENTS" in collapsed:
            locked.append(collapsed)

    bind = db.get_bind()
    sa_event.listen(bind, "after_cursor_execute", capture)
    try:
        response = client.post(f"/w/{session.access_token}/review", json={})
    finally:
        sa_event.remove(bind, "after_cursor_execute", capture)

    assert response.status_code == 200, response.text
    assert locked, "the current Event Order was read without FOR UPDATE"


# --- the same claim for the field with two spellings ---------------------------
#
# The Event Order prints ONE Music section (`music or music_entertainment`),
# so d7b2fbf reads a legacy document's merged value as `music` before both
# the comparison and the write. The words then arrive on a key the record
# does not describe -- and the very next line dropped the old name for a key
# that is now empty, leaving the words with an EMPTY record: has_record True,
# authored() empty, which is this module's positive encoding for "nothing
# here is a person's". The wizard's own outstanding item said the opposite.
#
# These go through the client's HTTP route deliberately. A test of the
# reading alone passes while the bug survives intact -- proved by mutation:
# the rename in place, the wizard's comparison reverted, and the record is
# empty again.

LEGACY_MUSIC = "Live band 8pm-11pm, then DJ. Sound limiter briefed."


def _booking_whose_music_is_in_the_old_spelling(db, loft, *, recorded=True, allergy=False):
    """An Event Order written before the music field was split: the detail
    lives in `music_entertainment`, there is no `music`, and a person wrote
    it. `recorded=False` is the document that predates the record itself."""
    booking = _make_booking(db, loft, event_date=dt.date(2027, 4, 10))
    change_status(db, booking, BookingStatus.confirmed, actor="test")

    content = generate_beo_content(booking)
    content["music"] = None
    content["music_entertainment"] = LEGACY_MUSIC
    if allergy:
        content["dietaries"] = ALLERGY
    if recorded:
        names = ["music_entertainment"] + (["dietaries"] if allergy else [])
        content = ca.record(content, names)
    documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")
    return booking


def _submit(db, booking, menu_items, client):
    session = wizard_service.get_or_create_session(db, booking, actor="client")
    _complete_all_steps(db, session, menu_items)
    response = client.post(f"/w/{session.access_token}/review", json={})
    assert response.status_code == 200, response.text
    db.expire_all()
    return response.json(), documents_service.get_current(db, booking.id, DocumentType.beo)


def test_a_promoted_music_value_keeps_its_authorship(db, loft, menu_items, client):
    """The words move to the key the Event Order prints, and the record
    moves with them. Before this they arrived unrecorded, and the document
    positively asserted that a person's words were the generator's."""
    booking = _booking_whose_music_is_in_the_old_spelling(db, loft)

    body, current = _submit(db, booking, menu_items, client)

    assert current.content["music"] == LEGACY_MUSIC, "the words the section prints"
    assert current.content["music_entertainment"] is None
    assert ca.authored(current.content) == {"music"}, (
        f"the record must describe where the words actually are: {current.content.get('_authored')}"
    )
    # ...and the note beside it says the same thing.
    assert any("Music kept from the previous Event Order" in item for item in body["outstanding_items"]), (
        body["outstanding_items"]
    )


def test_a_document_that_never_had_a_record_does_not_acquire_one(db, loft, menu_items, client):
    """Silence stays silence. Moving a name is a rename, never a new claim
    -- and conjuring one here would re-commit the reversal that got the
    2026-09-07 design reverted: a document nobody stamped claiming a person
    wrote it."""
    booking = _booking_whose_music_is_in_the_old_spelling(db, loft, recorded=False)

    _, current = _submit(db, booking, menu_items, client)

    assert current.content["music"] == LEGACY_MUSIC, "the words are still kept"
    assert ca.has_record(current.content) is False, (
        f"a record was invented: {current.content.get('_authored')}"
    )


def test_the_rename_leaves_every_other_recorded_name_alone(db, loft, menu_items, client):
    """A rename of one name must not be a rewrite of the record."""
    booking = _booking_whose_music_is_in_the_old_spelling(db, loft, allergy=True)

    _, current = _submit(db, booking, menu_items, client)

    assert ca.authored(current.content) == {"music", "dietaries"}, current.content.get("_authored")
    assert current.content["dietaries"] == ALLERGY
    assert current.content["music"] == LEGACY_MUSIC


def test_the_next_regenerate_still_sees_a_person_behind_the_words(db, loft, menu_items, client):
    """Why the record matters here at all: it is what a later reader
    consults to tell a person's words from the generator's."""
    booking = _booking_whose_music_is_in_the_old_spelling(db, loft)
    _, current = _submit(db, booking, menu_items, client)

    assert "music" in ca.authored(current.content)

    # And the guard still protects the value itself. Membership, not
    # equality: a bare rebuild from the booking also loses every other
    # answer the client gave through the wizard, so the row list is long
    # and asserting it exactly would be asserting what I meant rather than
    # what a regenerate does.
    fresh = generate_beo_content(booking, music="Something else entirely.")
    rows = {loss.field: loss for loss in dr.losses(db, current, fresh)}
    assert "music" in rows, sorted(rows)
    assert rows["music"].current == LEGACY_MUSIC
    assert rows["music"].incoming == "Something else entirely."
    assert "music_entertainment" not in rows, "still one question about one section"
