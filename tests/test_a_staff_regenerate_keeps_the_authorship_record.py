"""The record of who wrote a field survives a staff Regenerate.

content_authorship is what lets losses() tell "somebody cleared this on
purpose" apart from "nobody ever filled it in", and what lets the food
price guard tell a negotiated line apart from a catalogue one. It is
carried forward on every rebuild -- by the wizard, which did it inline in
two parts.

THE STAFF REGENERATE DID NEITHER PART, at any of its three write sites. So
every staff rebuild produced a version whose record was empty, and the
next rebuild read a person's words as the generator's. Two reachable
consequences, two clicks apart:

  A FIELD SOMEBODY CLEARED COMES BACK. The band cancels, Chloe clears
  Music on the draft Event Order and saves (v1 records `music`, empty).
  She hits Regenerate for some other change; the screen correctly offers
  Music as a loss and she keeps it. v2 holds the empty value and NO
  record. The next Regenerate has nothing saying a person emptied it, so
  losses() does not report it, and the Event Order names the DJ again with
  no question asked.

  A HAND-PRICED FOOD ORDER IS RE-PRICED. A platter negotiated down from
  the catalogue price is kept through the confirmation screen -- the value
  survives, the record does not -- and the price guard reads those lines
  as the catalogue's on the next pass.

The two steps are document_regeneration.carry_authorship now, called by
the wizard and by all three staff sites. A rule with no name is a rule
nobody can reuse, which is exactly how one path got it and the other
never did.
"""
import pytest
from sqlalchemy import select

from app.models.document import Document, DocumentStatus, DocumentType
from app.services import content_authorship, document_regeneration, documents as documents_service
from app.services.document_generation import generate_beo_content


def _beo(db, booking, **overrides):
    content = generate_beo_content(booking)
    content.update(overrides)
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )


def _current(db, booking):
    return documents_service.get_current(db, booking.id, DocumentType.beo)


def _regenerate(admin_client, booking, **data):
    """The straight-through Regenerate: no losses, no pending work."""
    page = admin_client.get(f"/admin/bookings/{booking.id}")
    import re

    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


# --- the helper itself --------------------------------------------------


def test_the_record_is_carried_forward(db, booking):
    # Generated with the generator's own value and then CHANGED, because
    # changed_fields records a name only when the value actually moves --
    # writing the same string twice records nothing, correctly, and a probe
    # built that way proves nothing about the carry.
    document = _beo(db, booking, special_notes="")
    documents_service.update_content_fields(
        db, document, {"special_notes": "Chloe's words"},
        actor="staff:chloe", authored_fields=["special_notes"],
    )
    assert "special_notes" in content_authorship.authored(_current(db, booking).content), (
        "the fixture did not record anything, so the carry below is untested"
    )

    fresh = generate_beo_content(booking)
    fresh["special_notes"] = "Chloe's words"
    carried = document_regeneration.carry_authorship(fresh, _current(db, booking))

    assert "special_notes" in content_authorship.authored(carried)


def test_a_name_whose_value_this_rebuild_replaced_is_forgotten(db, booking):
    """The half that stops the opposite error. Carrying alone would leave
    the record claiming a person wrote what the generator just produced."""
    document = _beo(db, booking, special_notes="")
    documents_service.update_content_fields(
        db, document, {"special_notes": "Chloe's words"},
        actor="staff:chloe", authored_fields=["special_notes"],
    )
    assert "special_notes" in content_authorship.authored(_current(db, booking).content)

    fresh = generate_beo_content(booking)
    fresh["special_notes"] = "something the generator produced"
    carried = document_regeneration.carry_authorship(fresh, _current(db, booking))

    assert "special_notes" not in content_authorship.authored(carried)


def test_a_first_version_has_nothing_to_carry(db, booking):
    fresh = generate_beo_content(booking)

    assert document_regeneration.carry_authorship(fresh, None) == fresh


# --- and every staff write site calls it --------------------------------


def test_a_straight_through_regenerate_keeps_the_record(admin_client, db, booking):
    """THE one. Nothing is at risk, so no screen appears -- and this is the
    path that silently emptied the record on every ordinary click."""
    # The booking says the same thing the staff member typed, so the
    # rebuild produces an IDENTICAL value and nothing is at risk -- which is
    # what routes this through the straight-through branch, the one that
    # shows no screen. It is also exactly the case the wizard's own comment
    # describes as the original defect: "a document whose recorded fields
    # were all unchanged lost its record entirely, and the next regenerate
    # treated those values as the generator's".
    booking.notes = "Ring the kitchen about the cake"
    db.flush()
    document = _beo(db, booking, internal_notes="")
    documents_service.update_content_fields(
        db, document, {"internal_notes": "Ring the kitchen about the cake"},
        actor="staff:chloe", authored_fields=["internal_notes"],
    )
    db.commit()
    assert "internal_notes" in content_authorship.authored(_current(db, booking).content)
    assert document_regeneration.losses(
        db, _current(db, booking), generate_beo_content(booking)
    ) == [], "something is at risk, so this is not the straight-through path"

    response = _regenerate(admin_client, booking)
    assert response.status_code in (200, 303)

    after = _current(db, booking)
    assert after.version > document.version, "no new version was written"
    assert "internal_notes" in content_authorship.authored(after.content), (
        "the staff Regenerate dropped the authorship record, so the next one "
        "will refill a field somebody cleared on purpose"
    )


def test_a_cleared_field_is_still_reported_as_a_loss_on_the_next_regenerate(
    admin_client, db, booking
):
    """The consequence, end to end and two rebuilds deep. This is what the
    record is FOR: without it the second rebuild has nothing saying a
    person emptied the field, so it refills it without asking."""
    document = _beo(db, booking, music="Live band 8pm-11pm")
    documents_service.update_content_fields(
        db, document, {"music": ""}, actor="staff:chloe", authored_fields=["music"],
    )
    db.commit()
    assert "music" in content_authorship.authored(_current(db, booking).content), (
        "clearing the field recorded nothing -- the rest of this probe would "
        "then be about a document that never had a record"
    )

    # The rebuild has a REAL music value to put back -- otherwise losses()
    # correctly reports nothing, because replacing an emptied field with a
    # placeholder is nothing-to-nothing and not a decision anybody needs.
    # Without this the probe would go green on a document that was never in
    # danger.
    def rebuilt():
        fresh = generate_beo_content(booking)
        fresh["music"] = "DJ from 8pm"
        fresh["music_entertainment"] = None
        return fresh

    # First rebuild: Music is offered as a loss and kept.
    first = _current(db, booking)
    losses = document_regeneration.losses(db, first, rebuilt())
    assert any(loss.field == "music" for loss in losses), (
        "the cleared field is not even reported the first time -- this test "
        "would prove nothing about the second"
    )

    kept = document_regeneration.apply_choices(rebuilt(), first, {"music"})
    kept = document_regeneration.carry_authorship(kept, first)
    second = documents_service.create_new_version(
        db, booking, DocumentType.beo, kept, actor="staff:test"
    )

    # Second rebuild: the same question must still be asked.
    again = document_regeneration.losses(db, second, rebuilt())
    assert any(loss.field == "music" for loss in again), (
        "a field cleared on purpose was refilled without asking on the second "
        "regenerate -- the record did not survive the first"
    )


def test_every_staff_write_site_carries_the_record():
    """Structural, and it is the honest shape here: there are three write
    sites and a behavioural probe of one says nothing about the other two.
    The defect WAS that one of several paths forgot."""
    import inspect

    from app.api import admin_bookings

    for name in ("generate_document", "generate_document_confirmed"):
        fn = getattr(admin_bookings, name, None)
        assert fn is not None, f"{name} was renamed -- point this test at it"
        src = inspect.getsource(fn)
        writes = src.count("create_new_version")
        carries = src.count("carry_authorship")
        assert carries >= writes, (
            f"{name} has {writes} create_new_version call(s) and {carries} "
            "carry_authorship call(s) -- a write without the carry drops the record"
        )
