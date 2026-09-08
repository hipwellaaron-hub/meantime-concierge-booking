"""Document versioning and lifecycle. A new version always supersedes the
previous one rather than overwriting it: the old row and its token are
never changed, so what a client was sent can always be read back by staff
(admin ... /preview shows any version).

That is about the RECORD, not about the client's link. Superseding a
version also stops its token resolving -- the public route gates on
is_current, so the client gets a 410 until the new version is sent. Both
Regenerate and Revise do this. Do not read "the row is untouched" as "the
link keeps working"; an earlier version of this docstring did, and so did
the comment on Document.is_current, and a design was scoped against the
wrong premise because of it.

Every transition is also logged to booking_events for the same reason
booking status changes are: no state change should require re-reading an
email chain to explain.
"""

import datetime as dt
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Document
from app.models.document import DocumentStatus, DocumentType
from app.services import booking as booking_service
from app.services import content_authorship
from app.utils import is_valid_email, truncate

logger = logging.getLogger(__name__)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def get_current(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    return db.execute(
        select(Document).where(
            Document.booking_id == booking_id,
            Document.type == doc_type,
            Document.is_current.is_(True),
        )
    ).scalar_one_or_none()


# How many times the locked read may come back empty while an unlocked
# read still finds a current document. See lock_current_for_update.
_LOCK_ATTEMPTS = 5


def lock_current_for_update(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    """The current document, locked FOR UPDATE.

    For a caller that reads the content, decides something from it, and
    then writes -- regenerating with a human's keep/replace choices is the
    one that exists. Without the lock, an approval committing between the
    read and the write is silently reverted and the audit line still says
    the value was kept (proved live with two sessions, 2026-09-06). The
    same shape update_content_fields already uses for the same reason.

    populate_existing is what makes the lock mean anything. Without it the
    ORM hands back whatever is already in this Session's identity map --
    the attributes as they were BEFORE the lock was granted -- so the row
    is locked and the caller reads pre-lock content anyway. Both callers
    prime the map first: the wizard through get_prior_beo_internal_notes,
    which reads this very row unlocked, and the staff regenerate through
    the page it renders from. The approval this was written to protect was
    therefore invisible, and the regenerate ran straight through.

    RETRIED, because a lock that waits its turn can be handed nothing at
    all. The statement takes its snapshot before the other transaction
    commits, so it sees only the row that transaction holds and blocks on
    it; when that commit lands, Postgres re-evaluates the row it was
    waiting on (EvalPlanQual), finds is_current now false, and skips it --
    and the replacement row is not in this statement's snapshot. The query
    returns NOTHING while a current document plainly exists.

    That empty answer is the dangerous one, because every caller reads it
    as "this booking has no document yet": losses() has nothing to compare
    against and returns [], no confirmation screen is shown, and the new
    version replaces a declared allergy with the generator's placeholder
    while the audit trail records a plain document_created and no
    regenerate note (proved through the real route with two threads on
    real Postgres -- 303 where the same race with this loop in place gives
    409 and keeps the allergy).

    A new statement gets a new snapshot at READ COMMITTED, so looking
    again is the whole repair -- WITHOUT committing or rolling back
    between attempts, which would end the caller's transaction and release
    every lock this request holds, including the one just taken. None is
    returned only once an unlocked read agrees there is no current row,
    which is the genuine first-generate case; exhausting the attempts
    means a current row exists and could not be locked, and saying
    "nothing here" to that is the silent overwrite this exists to stop.
    """
    for _ in range(_LOCK_ATTEMPTS):
        locked = db.execute(
            select(Document)
            .where(
                Document.booking_id == booking_id,
                Document.type == doc_type,
                Document.is_current.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if locked is not None:
            return locked
        if get_current(db, booking_id, doc_type) is None:
            return None
    raise RuntimeError(
        f"could not lock the current {doc_type.value} for booking {booking_id}: "
        f"a newer version was committed during each of {_LOCK_ATTEMPTS} attempts. "
        "Answering None would read as 'no document exists' and overwrite it."
    )


def is_mid_revision(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> bool:
    """Whether this booking's client is holding a link that no longer works.

    True when the current version of `doc_type` is a DRAFT and an earlier
    version was already sent: the public route gates on is_current, so the
    link the client has 410s, and the replacement has not gone out yet.

    QUERIED, not read off booking.documents. The relationship is only
    correct on a freshly loaded booking, and this is a notice about a
    client having no working link -- the one thing it must not do is fail
    to appear because a caller happened to hold a stale collection. It cost
    a test failure to find that out, which is the cheap way.

    TWO COLUMNS, not whole rows. The booking page calls this once per
    document type on every render, and selecting Document loaded the JSONB
    content of every version ever generated for the booking -- hundreds of
    KB across the wire to answer a two-boolean question about a document
    nobody is reading here.
    """
    versions = db.execute(
        select(Document.is_current, Document.status).where(
            Document.booking_id == booking_id, Document.type == doc_type
        )
    ).all()
    current = next((row for row in versions if row.is_current), None)
    if current is None or current.status != DocumentStatus.draft:
        return False
    return any(
        row.status in (DocumentStatus.sent, DocumentStatus.viewed, DocumentStatus.signed)
        for row in versions
        if not row.is_current
    )


REVISED_NOTE = "copied forward from the sent version and reopened for editing"


def revise(db: Session, document: Document, *, actor: str) -> Document:
    """Reopen a SENT document for editing by copying it forward.

    The edit form refuses anything that is not a draft, so until now the
    only way to change one word of a sent Event Order was Regenerate --
    which rebuilds from the booking and destroys whatever was typed. That
    is exactly how HAM-20260912-2R11Q lost a page of hand-entered content
    on 2026-09-07: saved at 23:41, regenerated at 23:52, gone.

    This copies the CURRENT content forward into a new draft. Nothing is
    rebuilt, so there is nothing to destroy -- losses() against a copy of
    itself is empty by construction rather than by a guard doing its job.

    What it costs the client is what Regenerate already costs them: their
    link dies the moment a new version exists, because the public route
    gates on is_current. It does NOT rewrite what they are holding
    underneath them, which is the thing that would be unacceptable.

    A SIGNED AGREEMENT is refused outright (Aaron's ruling, 2026-09-08): a
    signed contract is the client's evidence of what they agreed to, and
    changing one means a new agreement they sign again, not a quiet
    supersession that un-signs the gate on a confirmed booking at 11pm.

    ONLY THE CURRENT VERSION. Superseding does not change a row's status,
    so v1 of a twice-sent document is still `sent` and passed every check
    below -- revising it copied v1's content forward as v3 and discarded
    v2's. Proved by running it: v2's typed room layout was simply gone from
    the current version. No race is needed to get there; Revise, then the
    browser Back button, then Revise again is enough, and the second click
    throws away the draft the first one just made.

    Locked and re-read first, like every other write here that reads
    content and then writes from it -- and the row it locks is the CURRENT
    one, which is the row every other writer of this document contends on.
    Locking the passed row instead left a concurrent regenerate free to
    supersede it while this was reading it.

    Not deep-copied. That looks like it should be needed -- content is
    nested JSONB and this module's other writers all copy -- but
    create_new_version commits and refreshes, so the new version's content
    is reloaded from its own row and shares nothing with this one either
    way. I wrote the deepcopy, mutation-checked it, and it survived because
    there was nothing behind it.
    """
    current = lock_current_for_update(db, document.booking_id, document.type)
    if current is None or current.id != document.id:
        raise ValueError(
            f"this is v{document.version} of the {document.type.value} and it is no longer the "
            "current version"
            + (f" -- v{current.version} is" if current is not None else "")
            + ". Revising it would copy older content forward and supersede the newer version. "
            "Reload the booking and revise the current one."
        )
    # The locked, freshly-read row -- populate_existing means this is the
    # same object with the database's values, so nothing below reads a
    # field that was loaded before the lock was taken.
    document = current
    if document.is_legacy:
        raise ValueError(
            f"the current {document.type.value} is a legacy record of what was signed in iVvy -- "
            "it can't be revised; the signed original stands"
        )
    if document.type == DocumentType.agreement and document.status == DocumentStatus.signed:
        raise ValueError(
            "this agreement has been signed -- revising it would supersede the contract the client "
            "agreed to. Issue a new agreement for them to sign instead."
        )
    if document.status not in (DocumentStatus.sent, DocumentStatus.viewed):
        raise ValueError(
            f"only a sent {document.type.value} needs revising -- this one is "
            f"{document.status.value}"
            + (" and can be edited directly" if document.status == DocumentStatus.draft else "")
        )
    return create_new_version(
        db,
        document.booking,
        document.type,
        dict(document.content),
        actor=actor,
        revised_note=REVISED_NOTE,
    )


def content_fingerprint(content: object, fields: Iterable[str]) -> str:
    """Identifies the values an edit form was rendered from.

    The form carries this back and the save refuses if it no longer
    matches -- a compare-and-set, the same shape as the regenerate screen's
    `expect` and every other toggle in this codebase.

    The row lock alone cannot do this job. The staleness does not come from
    a race inside the save; it comes from the GET that rendered the form,
    which may be minutes old. A lock taken during the POST closes the
    window between this request's read and its write, and nothing about the
    window between the page load and the save -- so without this a value
    somebody else committed in between is silently reverted, and now also
    recorded as the reverting staff member's own words.

    WHAT IS COVERED, precisely: whatever `fields` names, and nothing else.
    Both callers pass the protected free-text fields, so this protects the
    prose and NOT the rest of the form.

    That is narrower than the form, and the difference is a real hole. The
    edit form also writes the food order line items, the vendor rows, the
    key moments, the guest arrival time and the AV block. A colleague's
    change to any of those moves nothing this looks at, so the save is
    accepted and reverts them without a word -- proved by putting four
    platters on a document through one form and one platter through
    another: the second save returned 303 and the quantity went back to
    one, with the food total recomputed from the stale line (review of
    85e326f). Money, on a client-facing document.

    Widening it is deliberate follow-up work rather than a line change,
    because it starts refusing saves that today succeed, and the vendor
    snapshot is rewritten by a different staff action entirely (the
    bump-in confirmation) -- fingerprinting it naively would collide with
    every open edit form and reject saves that conflict with nothing. Until
    then those fields remain last-write-wins, exactly as they were before
    this commit, and the conflict banner says so.
    """
    values = content if isinstance(content, dict) else {}
    # Canonical JSON over the whole field list at once. sort_keys settles
    # dict key order, which repr() leaves to insertion order and which would
    # otherwise let the same content fingerprint two ways and reject a save
    # that conflicts with nothing. One JSON document rather than a value at a
    # time, so no separator byte is needed to stop a value running into the
    # next name -- the brackets already do that. default=str keeps a stray
    # non-JSON value from raising here, since this is a comparison and never
    # a stored artefact.
    payload = json.dumps(
        [[name, values.get(name)] for name in sorted(fields)], sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def lock_draft_for_update(db: Session, document: Document) -> Document:
    """Take the row lock BEFORE the caller reads the content it is about to
    edit, and re-check that it is still a draft.

    update_content takes this lock itself, but by then a caller that built
    its new content from an earlier read has already lost: the value it is
    about to write was decided against a version that may have moved. That
    was survivable while the write was only last-write-wins -- one staff
    member silently reverting another, which the edit screen has always
    done. It stopped being survivable when the writer began RECORDING
    authorship, because the reverted value is then marked as the reverting
    staff member's own words and a later regenerate preserves it. A lost
    update became a protected lost update.

    So callers that read, decide, and then write take the lock here first,
    making the read and the write one critical section -- the same shape
    update_content_fields already has for the same reason.
    """
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot edit a document that is already {document.status.value} -- only a draft can be edited")
    return document


def get_by_token(db: Session, token: str) -> Document | None:
    return db.execute(select(Document).where(Document.access_token == token)).scalar_one_or_none()


def create_new_version(
    db: Session,
    booking: Booking,
    doc_type: DocumentType,
    content: dict,
    *,
    actor: str,
    regenerated_note: str | None = None,
    revised_note: str | None = None,
) -> Document:
    """`regenerated_note` records what a human chose to keep and what they
    let go when this version replaced one carrying their own words. It is
    written in the SAME transaction as the version: committing the version
    first and the note afterwards would let a crash in between leave the
    values discarded with no record of the decision -- and that record is
    Aaron's stated measure of whether the feature is working.

    `revised_note` is the other shape: this version was COPIED from the one
    before it rather than rebuilt from the booking. The trail has to tell
    the two apart -- `document_created` alone cannot, and
    `document_regenerated` is only written when a regenerate actually lost
    something, so a revise would otherwise be indistinguishable from a
    regenerate that happened to lose nothing."""
    if booking.parent_booking_id is not None:
        # A linked child (see app.services.booking.add_linked_space) is
        # just a second room for the parent's event -- its own documents
        # would duplicate the parent's, not describe anything real.
        raise ValueError("cannot create a document on a linked booking -- use the parent booking instead")
    previous = get_current(db, booking.id, doc_type)
    if previous is not None and previous.is_legacy:
        # A legacy record is a fixed copy of what was signed in iVvy -- never
        # regenerated over. If the booking has genuinely changed, the admin
        # is warned of the mismatch and re-papers deliberately, not by an
        # automatic regenerate that would bury the signed original.
        raise ValueError(
            f"the current {doc_type.value} is a legacy record of what was signed in iVvy -- "
            "it can't be regenerated over; the signed original stands"
        )
    next_version = 1
    if previous is not None:
        previous.is_current = False
        next_version = previous.version + 1
        db.flush()  # clear the partial-unique-index slot before the new current row claims it

    document = Document(
        booking_id=booking.id,
        type=doc_type,
        version=next_version,
        content=content,
        status=DocumentStatus.draft,
        is_current=True,
    )
    db.add(document)
    db.flush()

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type="document_created",
            field_name=f"{doc_type.value}_version",
            old_value=str(previous.version) if previous else None,
            new_value=str(next_version),
            actor=actor,
        )
    )
    if regenerated_note is not None:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="document_regenerated",
                field_name=f"{doc_type.value}_version",
                old_value=str(previous.version) if previous else None,
                new_value=truncate(f"v{next_version}: {regenerated_note}", 500),
                actor=actor,
            )
        )
    if revised_note is not None:
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="document_revised",
                field_name=f"{doc_type.value}_version",
                old_value=str(previous.version) if previous else None,
                new_value=truncate(f"v{next_version}: {revised_note}", 500),
                actor=actor,
            )
        )
    db.commit()
    db.refresh(document)

    if doc_type == DocumentType.agreement and previous is not None and previous.status == DocumentStatus.signed:
        # Superseding a signed agreement voids the client's signature. If the
        # booking was confirmed on that signature, a gate is lost: flag it
        # for a human, never move it (see booking.flag_if_confirmed_gate_lost).
        booking_service.flag_if_confirmed_gate_lost(db, booking, actor=actor)
    return document


# Event types whose `old_value` holds the NAMES of the fields a save
# changed rather than a previous value. Written by _fields_this_save_changed
# below and read by the booking page's audit table, which has a column
# headed "Old" -- without this the trail printed "Old: dietaries,
# room_layout_notes / New: 3", which parses as the version changing from
# those names into 3. That is the screen used to reconstruct exactly the
# incidents this record exists to explain, so it is the last one that may
# read wrong.
FIELD_LIST_IN_OLD_VALUE = ("document_edited", "beo_proposal_applied")


def _fields_this_save_changed(stored: object, incoming: dict) -> str | None:
    """The names of the content keys a save actually changed, for the audit.

    The trail recorded THAT a version was hand-edited and never WHICH fields
    it touched, so "did that edit change the food order?" could not be
    answered from the log at all. On 2026-09-08 it had to be inferred by
    comparing a document's food total against an invoice cut before the
    edit -- arithmetic, on a question the log should simply answer.

    differing_fields, not changed_fields: writing the generator's own
    placeholder over somebody's real text IS a change worth recording, and
    changed_fields deliberately skips it.

    `_authored` never appears, and this does not filter for it: every name
    goes through content_authorship._names_to_write, which refuses anything
    starting with an underscore. A second guard here would be one nothing
    can break -- I wrote one, mutation-checked it, and it survived because
    there was nothing behind it.
    """
    before = stored if isinstance(stored, dict) else {}
    changed = content_authorship.differing_fields(
        before, incoming, candidates=set(before) | set(incoming)
    )
    return truncate(", ".join(sorted(changed)), 500) or None


def update_content(
    db: Session,
    document: Document,
    content: dict,
    *,
    actor: str,
    authored_fields: Iterable[str] = (),
    placeholders: Iterable[str] = (),
) -> Document:
    """Hand-edit a draft's content in place. Draft-only, for the same
    reason delete_draft is: a draft was never shown to a client, so
    there's nothing for a client to have seen change under them. Anything
    already sent/viewed/signed must go through create_new_version()
    instead -- a link a client already holds must never silently start
    resolving to different words.

    Deliberately NOT a new version per save: one audit event per edit is
    enough to know it was hand-edited, by whom and when, without spawning
    a version per keystroke. Regenerating afterward still discards the
    edit and re-derives from the booking, exactly as before.

    `authored_fields` names the keys that hold a person's prose. Whichever
    of them this save actually CHANGES is recorded as human-written, so a
    later regenerate can tell the two apart instead of guessing. Callers
    that are refreshing machine-derived values -- a vendor snapshot, a
    rebuilt timeline -- pass nothing, which is the default: recording
    those as somebody's words would freeze exactly the content a
    regenerate exists to rebuild.

    `placeholders` are the values the generator writes when nothing was
    captured; a field set to one of them is never recorded as anyone's.

    A caller that built `content` from an earlier read should take
    lock_draft_for_update first, or the authorship it records may describe
    a value it is unknowingly reverting.
    """
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot edit a document that is already {document.status.value} -- only a draft can be edited")

    written = content_authorship.changed_fields(
        document.content, content, candidates=authored_fields, placeholders=placeholders
    )
    # Read BEFORE the content is replaced, and before `record` rewrites it.
    changed = _fields_this_save_changed(document.content, content)
    # Recorded LAST and assigned, per the module's caller rules: `record`
    # deep-copies, so what it returns is a value SQLAlchemy compares
    # unequal to the one it loaded, and the UPDATE is actually emitted.
    document.content = content_authorship.record(content, written)
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type="document_edited",
            field_name=f"{document.type.value}_version",
            # WHICH fields moved. new_value stays the version number and
            # field_name stays "<type>_version": three readers match on
            # exactly those two (document_regeneration.was_hand_edited and
            # _last_hand_edit_at, and the production audit), so this goes in
            # the one column document_edited leaves empty.
            old_value=changed,
            new_value=str(document.version),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(document)
    return document


def update_content_fields(
    db: Session,
    document: Document,
    changes: dict,
    *,
    actor: str,
    event_type: str = "document_edited",
    authored_fields: Iterable[str] = (),
    placeholders: Iterable[str] = (),
) -> Document:
    """Merge specific keys into a draft's content, reading it AFTER the row
    lock is taken.

    `event_type` names what kind of change this was. The default is a
    hand-edit; an approval applying an AI proposal passes its own, because
    "document_edited" is what the regenerate screen reads as "a person
    typed into this version" -- and an approval is not that (ultrareview,
    2026-09-06: every approval was making the screen claim a hand-edit).

    update_content above takes a whole content dict the caller built from
    an earlier read, so two callers editing different keys last-write-wins
    -- one silently reverts the other while both believe they succeeded
    (2026-09-06 review). This exists for callers that know exactly which
    keys they are changing: the lock, the read and the write are one
    critical section, so a concurrent change to a different key survives.

    `authored_fields` names the keys that hold a person's prose; see
    update_content above.
    """
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot edit a document that is already {document.status.value} -- only a draft can be edited")

    # Diff BEFORE merging -- it needs the values being replaced -- but
    # record AFTER, against the merged content. Recording against `changes`
    # would produce a record naming only those keys, and merging that over
    # the stored content erases every other name (caller rule 1).
    content = dict(document.content)
    written = content_authorship.changed_fields(
        content, changes, candidates=authored_fields, placeholders=placeholders
    )
    # Against `changes` alone: this call merges specific keys, so the keys it
    # did not touch are not part of what it changed.
    changed = _fields_this_save_changed(content, changes)
    content.update(changes)
    document.content = content_authorship.record(content, written)
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type=event_type,
            field_name=f"{document.type.value}_version",
            old_value=changed,
            new_value=str(document.version),
            actor=actor,
        )
    )
    db.commit()
    db.refresh(document)
    return document


def _transition(db: Session, document: Document, new_status: DocumentStatus, *, actor: str) -> Document:
    old_status = document.status
    document.status = new_status
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type="document_status_changed",
            field_name="status",
            old_value=old_status.value,
            new_value=new_status.value,
            actor=actor,
        )
    )
    db.commit()
    db.refresh(document)
    return document


def mark_sent(db: Session, document: Document, *, actor: str) -> Document:
    """Nothing auto-sends: this is the explicit human action that makes a
    draft visible at its public link -- staff then paste that link into
    their own email to the client. Refuses to make that link "sent" at all
    if there's nowhere to actually send it: a missing contact or a
    malformed address would make the sent/viewed/signed status lie about
    a client ever having a chance to see it, rather than genuinely
    reflecting what happened."""
    # Locks the row for the rest of this transaction: two concurrent calls
    # (e.g. a double-clicked "send" in a future admin UI) must not both
    # pass a stale in-Python status check and both append a transition.
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot send a document that is already {document.status.value}")
    contact = document.booking.contact
    if contact is None or not is_valid_email(contact.email):
        raise ValueError(
            "cannot send: this booking has no contact with a valid email address on file"
        )
    document = _transition(db, document, DocumentStatus.sent, actor=actor)

    if document.type == DocumentType.agreement:
        # Sending the agreement is half of what holds the date; the deposit
        # invoice is the other half (see booking.auto_hold_on_send). After
        # the transition is committed and never raising -- the send must
        # stand even if the hold can't proceed (e.g. a room clash), which is
        # surfaced as a review flag instead.
        try:
            booking_service.auto_hold_on_send(db, document.booking, actor=actor)
        except Exception:  # noqa: BLE001 -- see above; a failure here must not undo a real send
            logger.exception("Auto-hold after sending agreement failed for document %s", document.id)
    return document


def record_view(db: Session, document: Document) -> Document:
    """Called on the client's first GET of the public link. Only moves
    sent -> viewed; never regresses an already-viewed or signed document.
    viewed_at is set here too, once -- status alone says "was this ever
    opened", the timestamp says when."""
    db.refresh(document, with_for_update=True)
    if document.status == DocumentStatus.sent:
        document.viewed_at = dt.datetime.now(dt.timezone.utc)
        return _transition(db, document, DocumentStatus.viewed, actor="client (auto)")
    return document


def delete_draft(db: Session, document: Document, *, actor: str) -> None:
    """Only a draft can be deleted. A draft was never shown to a client --
    there's nothing to preserve. Anything sent/viewed/signed must go
    through create_new_version() instead (superseded, never deleted), so a
    link that was already given to a client can never stop resolving to
    something."""
    if document.status != DocumentStatus.draft:
        raise ValueError(
            f"cannot delete a document that is already {document.status.value} -- only a draft can be deleted"
        )
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type="document_deleted",
            field_name=f"{document.type.value}_version",
            old_value=str(document.version),
            actor=actor,
        )
    )
    db.delete(document)
    db.commit()


def sign(db: Session, document: Document, *, signer_name: str, signer_ip: str) -> Document:
    # The critical case this guards: two near-simultaneous POSTs to
    # /d/{token}/sign (a double-clicked Accept & Sign button, or a client
    # retrying an ambiguous/slow response). Without this lock, both
    # requests could read status="sent" before either commits, both pass
    # the check below, and the second to commit would silently overwrite
    # the first's signer_name/signed_at with no error to either party.
    db.refresh(document, with_for_update=True)
    if document.status not in (DocumentStatus.sent, DocumentStatus.viewed):
        raise ValueError(f"cannot sign a document with status {document.status.value}")

    document.signed_at = dt.datetime.now(dt.timezone.utc)
    document.signer_name = signer_name
    document.signer_ip = signer_ip
    actor = truncate(f"client:{signer_name}", BOOKING_EVENT_ACTOR_MAX_LENGTH)
    document = _transition(db, document, DocumentStatus.signed, actor=actor)

    if document.type == DocumentType.agreement:
        # Signing is half of what confirms a booking; the deposit is the
        # other half (see app.services.booking.auto_confirm_if_ready).
        # Runs after the signature is committed and never raises: the
        # client's signature must stand even if confirmation can't
        # proceed, and they must not see an error for it.
        try:
            booking_service.auto_confirm_if_ready(db, document.booking, actor=actor)
        except Exception:  # noqa: BLE001 -- see above; a failure here must not undo a real signature
            logger.exception("Auto-confirm after signing failed for document %s", document.id)

        # Alert the venue that the agreement is signed. After auto-confirm
        # so the email can say whether this signature has tipped the
        # booking into confirmed. Never raises (see notify_agreement_signed).
        from app.models.booking import BookingStatus
        from app.services import notifications

        booking = document.booking
        notifications.notify_agreement_signed(
            booking,
            signer_name=signer_name,
            deposit_paid=booking_service.has_paid_deposit(db, booking),
            now_confirmed=booking.status == BookingStatus.confirmed,
        )

    return document


def get_beos_awaiting_review(db: Session, venue) -> list[Document]:
    """Current BEO drafts on live bookings: prepared but not yet sent.

    Self-clearing in the same way every other worklist in this app is --
    it reads current state rather than a delta, so sending the BEO (or
    the booking going terminal) drops it off with nothing to tick.

    Deliberately not restricted to wizard-generated BEOs. A draft BEO is
    awaiting review whether a client's wizard produced it or a staff
    member generated it by hand, and a list that quietly omitted half of
    them would be worse than no list.
    """
    from app.models import Booking, Space
    from app.services.booking import TERMINAL_STATUSES

    return list(
        db.scalars(
            select(Document)
            .join(Booking, Document.booking_id == Booking.id)
            .join(Space, Booking.space_id == Space.id)
            .where(
                Space.venue_id == venue.id,
                Document.type == DocumentType.beo,
                Document.status == DocumentStatus.draft,
                Document.is_current.is_(True),
                Booking.status.notin_(TERMINAL_STATUSES),
            )
            .order_by(Booking.event_date)
        ).all()
    )
