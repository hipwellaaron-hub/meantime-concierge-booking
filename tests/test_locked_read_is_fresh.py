"""A lock that hands back pre-lock content is not a lock.

lock_current_for_update exists so a caller can read the current document,
decide from it, and write on that decision without an approval landing in
between and being reverted. It took the row lock correctly and then
returned whatever was already in the Session's identity map -- the
attributes as they were BEFORE the lock was granted.

Both callers prime that map before locking. The wizard reads this very row
unlocked through get_prior_beo_internal_notes; the staff regenerate renders
a page from it. So in both, the lock was held and the decision was made on
stale content.

This repair existed on 2bbd23e and went away with the revert of that
commit, along with seven other repairs to pre-existing bugs. It is being
re-done deliberately rather than rediscovered.
"""

from sqlalchemy import text

from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.document_generation import generate_beo_content

ALLERGY = "1x severe nut allergy (table 4)."


def _write_underneath(db, document_id, value):
    """Change the row the way another transaction would have, without going
    through the ORM, so this Session's identity map still holds the old
    attributes -- exactly the state a concurrent commit leaves behind."""
    db.execute(
        text(
            "UPDATE documents SET content = jsonb_set(content, '{dietaries}', "
            "to_jsonb(cast(:v as text))) WHERE id = cast(:i as uuid)"
        ),
        {"v": value, "i": str(document_id)},
    )


def test_the_locked_read_returns_what_the_row_actually_holds(db, booking):
    """The whole point. Before this the assertion below returned 'none' --
    the value from before the lock -- so a caller deciding what to protect
    could not see the approval it was locking against."""
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**generate_beo_content(booking), "dietaries": "none"}, actor="test"
    )
    assert document.content["dietaries"] == "none", "loaded into the identity map"

    _write_underneath(db, document.id, ALLERGY)

    locked = documents_service.lock_current_for_update(db, booking.id, DocumentType.beo)

    assert locked is not None
    assert locked.content["dietaries"] == ALLERGY, (
        "the lock was granted but the content came from before it"
    )


def test_it_is_still_the_same_instance_the_session_is_tracking(db, booking):
    """populate_existing refreshes the instance rather than returning a
    second copy. A caller that goes on to write through the object it
    already held must be writing to the same one, or the write lands on a
    detached copy and vanishes."""
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**generate_beo_content(booking), "dietaries": "none"}, actor="test"
    )
    _write_underneath(db, document.id, ALLERGY)

    locked = documents_service.lock_current_for_update(db, booking.id, DocumentType.beo)

    assert locked is document, "the Session keeps one instance per row"
    assert document.content["dietaries"] == ALLERGY, "and the caller's own handle is fresh too"


def test_an_unlocked_read_beforehand_does_not_poison_the_locked_one(db, booking):
    """The shape both real callers have: read unlocked, then lock. The
    wizard does exactly this through get_prior_beo_internal_notes."""
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, {**generate_beo_content(booking), "dietaries": "none"}, actor="test"
    )
    primed = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert primed.content["dietaries"] == "none"

    _write_underneath(db, document.id, ALLERGY)

    locked = documents_service.lock_current_for_update(db, booking.id, DocumentType.beo)

    assert locked.content["dietaries"] == ALLERGY
