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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Document
from app.models.document import DocumentStatus, DocumentType
from app.services import booking as booking_service
from app.services import content_authorship
from app.utils import is_valid_email, truncate

logger = logging.getLogger(__name__)

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def lock_booking_row(db: Session, booking_id: uuid.UUID) -> None:
    """Serialise "make the FIRST version" on a row that always exists.

    lock_current_for_update locks the current document row -- and with no
    Event Order there is no row, so it locks nothing (SELECT ... FOR UPDATE
    over zero rows is a no-op). A staff Generate and an AI proposal racing
    to create v1 then both saw None; the loser hit the unique index and
    the caller got a 500 (proved 2026-09-11). Every path that may create a
    first version takes this lock BEFORE its locked read of the current
    row, so the second arrival sees the first one's draft."""
    db.execute(select(Booking.id).where(Booking.id == booking_id).with_for_update())


def get_current(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    return db.execute(
        select(Document).where(
            Document.booking_id == booking_id,
            Document.type == doc_type,
            Document.is_current.is_(True),
        )
    ).scalar_one_or_none()


def version_rows(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> list:
    """Every version of this type as (id, version, status, is_current,
    is_legacy), NEWEST FIRST. Narrow on purpose -- no content, no
    legacy_file -- for the same reason as is_mid_revision below: the floor
    list asks this once per booking to answer three booleans, and loading
    whole rows meant every version's JSONB content crossing the wire for a
    document nobody is reading there. Callers that render a version load
    it by id afterwards."""
    return db.execute(
        select(Document.id, Document.version, Document.status, Document.is_current, Document.is_legacy)
        .where(Document.booking_id == booking_id, Document.type == doc_type)
        .order_by(Document.version.desc())
    ).all()


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

    An APPROVED EVENT ORDER (status signed, 2026-09-10) CAN be revised. The
    approved version is never touched; the copy starts as a draft, goes out
    again, and needs the client's approval again, and the trail records
    that an approval was set aside. An Event Order changes right up to the
    day, and this is the safe way to change one. The next paragraph is the
    contrast that makes that a decision rather than an accident.

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
    # An APPROVED Event Order (status signed) can be revised. This is the
    # one deliberate difference from the agreement rule above: a contract
    # is what the client agreed to and stands; an Event Order changes
    # right up to the day, and Revise is the safe way to change it. The
    # approved version is never touched -- the copy starts as a draft, goes
    # out again, and needs the client's approval again (Aaron, 2026-09-10:
    # approval "locks it", and a change is a new version they approve).
    revisable = (DocumentStatus.sent, DocumentStatus.viewed) + (
        (DocumentStatus.signed,) if document.type == DocumentType.beo else ()
    )
    if document.status not in revisable:
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

    AND that it is still the CURRENT version, which is a separate question.
    Superseding a row does not change its status, so a draft that a
    regenerate replaced is still a draft and satisfied the check above --
    the row is locked by id, so the lock is granted on a version nobody
    will ever read again. Proved by running it: a staff member typing into
    an edit form opened before someone regenerated got a 303 and their
    words landed on a row whose is_current is False, while the live Event
    Order still said "[REVIEW] add room layout notes". Content typed by a
    human, gone, and the redirect indistinguishable from a successful save.

    beo_proposals._locked_draft has made exactly this check since
    2026-09-06 with a comment saying it was proved live then. It was never
    carried across to the edit form, which is the busier path.
    """
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot edit a document that is already {document.status.value} -- only a draft can be edited")
    if not document.is_current:
        raise ValueError(
            f"this is v{document.version} of the {document.type.value} and a newer version has "
            "replaced it -- saving here would write onto a version nobody can read. Reload the "
            "booking and edit the current one."
        )
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
    commit: bool = True,
    supersede_signed: bool = False,
) -> Document:
    """`commit=False` leaves the transaction to the caller: a proposal that
    creates the first Event Order draft writes the draft and itself
    together, so neither can exist without the other. Event Orders only --
    the agreement branch below runs after the commit and needs one.

    `regenerated_note` records what a human chose to keep and what they
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
    if not commit and doc_type != DocumentType.beo:
        raise ValueError("commit=False is for Event Order drafts only")
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
    # THE SAME RULE FOR A NATIVELY SIGNED AGREEMENT, which is what the
    # legacy branch above has always said and never covered.
    #
    # revise() refuses this outright (see its message: "revising it would
    # supersede the contract the client agreed to"). That refusal was never
    # carried here -- so Regenerate did it silently, flipped is_current on
    # the signed row and raised a review flag only afterwards. McKenzi
    # Mostyn (HAM-20260920-I0K8G) lost her signed agreement that way, with
    # a paid deposit and the event nine days out.
    #
    # NOT an outright refusal, because Generate is the only way to issue a
    # replacement agreement -- revise() itself says "Issue a new agreement
    # for them to sign instead", and a flat refusal here would make that
    # sentence false. It is an opt-in: the caller has to say it means to,
    # which turns a silent side effect into a decision with a name on it.
    superseding_signed_agreement = (
        previous is not None
        and doc_type == DocumentType.agreement
        and previous.status == DocumentStatus.signed
    )
    if superseding_signed_agreement and not supersede_signed:
        raise ValueError(
            f"this agreement was signed"
            + (f" by {previous.signer_name}" if previous.signer_name else "")
            + " -- regenerating supersedes the contract the client agreed to, and the booking is "
            "left with an unsigned draft until they sign again. Confirm on the booking page if "
            "that is what you mean to do."
        )
    # Count from the highest version that EXISTS, not from the current one.
    # Those are the same number whenever a current version exists, and they
    # differ in exactly the case that used to be unrecoverable: a booking
    # left with NO current version (a draft deleted after it superseded a
    # sent one, before 2026-09-11) counted from nothing, retried version 1,
    # and hit uq_document_booking_type_version. The booking page only offers
    # Delete on the CURRENT document, so there was no way back through the
    # app -- that booking could never be given another document at all.
    highest = db.execute(
        select(func.max(Document.version)).where(
            Document.booking_id == booking.id,
            Document.type == doc_type,
        )
    ).scalar()
    next_version = (highest or 0) + 1
    if previous is not None:
        previous.is_current = False
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
    if doc_type == DocumentType.beo and previous is not None and previous.status == DocumentStatus.signed:
        # The client had APPROVED the version this replaces. Written in the
        # SAME transaction as the version, per this function's own rule
        # above: a crash between the two must not leave an approval set
        # aside with no record of it. Nothing is
        # blocked -- Event Orders change until the day -- but the trail has
        # to say an approval was set aside, because the new version goes out
        # unapproved and the floor must not treat it as agreed.
        db.add(
            BookingEvent(
                booking_id=booking.id,
                event_type="beo_approval_superseded",
                field_name="beo_version",
                old_value=str(previous.version),
                new_value=truncate(
                    f"v{document.version} replaces v{previous.version}, which "
                    f"{previous.signer_name or 'the client'} had approved -- needs approving again",
                    500,
                ),
                actor=actor,
            )
        )
    if not commit:
        db.flush()
        return document
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
# Deliberately not listed: beo_proposal_created and beo_proposal_blocked put
# the AI's SOURCE TEXT in old_value (field names go in field_name), which is
# a third shape again, not a field list. Naming them here would make this
# constant lie about what it selects; their display is a separate question.
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
    if not document.is_current:
        # Same trap as lock_draft_for_update above: superseding does not
        # change a status, so a replaced draft is still a draft. Sending it
        # flipped the row to `sent` -- proved by running it -- while the
        # public route gates on is_current, so the link staff had just
        # "sent" 410s. For an agreement it also takes a room hold
        # (auto_hold_on_send below) off the back of a document the client
        # can never open.
        raise ValueError(
            f"this is v{document.version} of the {document.type.value} and a newer version has "
            "replaced it -- its link would not work. Reload the booking and send the current one."
        )
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
    something.

    That promise was only half true until 2026-09-11: deleting a draft
    that had SUPERSEDED a sent version left no current version, and the
    already-issued link stopped resolving. Abandoning a REVISE now restores
    the version it superseded. Abandoning a REGENERATE does not, and must
    not -- see the comment on the restore below for why those two differ
    and what the Regenerate case still costs."""
    if document.status != DocumentStatus.draft:
        raise ValueError(
            f"cannot delete a document that is already {document.status.value} -- only a draft can be deleted"
        )
    # Deleting a draft that a REVISE created is an undo of that Revise, so
    # the version it superseded becomes current again. A Regenerate draft
    # is not an undo and is handled differently below.
    #
    # Without this the booking is left with NO current version at all, and
    # because every public link gates on is_current (_is_live in
    # app/api/documents.py), the link the client is ALREADY HOLDING stops
    # resolving -- permanently, and with the wrong sentence. Proved over
    # HTTP before this existed: send an agreement, press Revise, delete
    # the draft, and the client's link answers "This link is no longer
    # active. Get in touch and we'll help directly" on a document that was
    # genuinely sent and is still the latest thing they were given.
    # Nobody is told, and there is no way back through the UI.
    #
    # The floor app already carries a fallback for exactly this state (see
    # app/api/staff_app.py, "a draft deleted so that no version is
    # current"). That stays as defence in depth, but the invariant belongs
    # here, where it is broken, rather than in each reader that trips over
    # it -- the floor was only ever one of the readers.
    # NO extra lock is taken here, deliberately. An earlier draft of this
    # fix called lock_booking_row first, which took the booking row FOR
    # UPDATE before touching the document row -- the OPPOSITE order to
    # update_content, mark_sent and the wizard's create_new_version, all of
    # which take the document row first and then need FOR KEY SHARE on the
    # booking via the booking_events foreign key. That is a real ABBA cycle
    # and it deadlocked on Postgres in review, with the victim getting a
    # bare 500. Reversing it is not the answer either: taking the document
    # row first deadlocks against Regenerate, which IS booking-row-first
    # (admin_bookings.py:608). The module has a pre-existing lock-ordering
    # disagreement and settling it is its own change; until then this stays
    # exactly where it was, which is proven not to deadlock.
    booking_id, doc_type = document.booking_id, document.type
    deleted_version = document.version
    restored = None

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
    # Free the partial-unique-index slot before the restored row claims it,
    # the mirror of the flush create_new_version does when it supersedes.
    db.flush()

    # Two conditions, and the second one is the whole finding.
    #
    # The slot must actually be EMPTY: a draft that was already superseded
    # never held it, so nothing moves.
    #
    # And the deleted draft must have come from REVISE, not Regenerate.
    # Both produce a draft, and only one is an undo. revise() copies the
    # current content forward verbatim, so abandoning it genuinely restores
    # what the client is already holding. Regenerate REBUILDS from the
    # booking, which is why staff reached for it -- the booking changed --
    # so the version underneath is stale by definition. Restoring after an
    # abandoned Regenerate let a client open and SIGN a superseded
    # agreement printing the old date and headcount while the booking said
    # otherwise, and has_signed_agreement then asserted the deal was
    # papered. Proved end to end through the admin UI in review, against a
    # control, and shown to be newly reachable: before this restore existed
    # that link answered 410 and the signature was refused.
    #
    # The discriminator is the document_revised trail row, which revise()
    # always writes (REVISED_NOTE) in the same transaction as the version.
    # NOT the absence of document_regenerated: the straight-through
    # Regenerate path writes no note at all, so that test would miss
    # exactly the case above.
    #
    # Abandoning a Regenerate therefore leaves the slot empty and the
    # client's link dead, which is what HEAD already did -- no better, and
    # deliberately not worse. It is written up as its own item rather than
    # guessed at here, because the honest answer for that case is to let
    # the link resolve while refusing the signature, and that needs the
    # drift comparison this module does not yet have for generated
    # documents.
    came_from_revise = db.execute(
        select(BookingEvent.id)
        .where(
            BookingEvent.booking_id == booking_id,
            BookingEvent.event_type == "document_revised",
            BookingEvent.field_name == f"{doc_type.value}_version",
            BookingEvent.new_value.startswith(f"v{deleted_version}: "),
        )
        .limit(1)
    ).first() is not None

    if came_from_revise and get_current(db, booking_id, doc_type) is None:
        restored = db.execute(
            select(Document)
            .where(Document.booking_id == booking_id, Document.type == doc_type)
            .order_by(Document.version.desc())
            .limit(1)
        ).scalars().first()
        # Unreachable by construction, and stated rather than mutation-
        # tested because there is no way to reach it: came_from_revise is
        # only true when revise() built this draft FROM a previous version,
        # and revise never deletes that version, so a row always remains.
        # It stands as the answer to "what if the impossible happens" --
        # skip quietly rather than raise AttributeError inside a delete.
        if restored is not None:
            restored.is_current = True
            db.add(
                BookingEvent(
                    booking_id=booking_id,
                    event_type="document_current_restored",
                    field_name=f"{doc_type.value}_version",
                    new_value=str(restored.version),
                    actor=actor,
                )
            )
    db.commit()

    # NO auto-confirm call here, and the reason is worth writing down.
    # Review found that restoring a SIGNED agreement hands back both halves
    # of the confirmation gate while nothing re-checks it, leaving a booking
    # at tentative holding a signed agreement and a paid deposit. That was
    # real against the first version of this fix, which restored after a
    # REGENERATE too. Making the restore Revise-only closed it: revise()
    # refuses a signed agreement outright (see its own guard above), so a
    # restored agreement can never be a signed one. An approved Event Order
    # CAN be revised and restored, and the confirmation gate does not read
    # Event Orders. Adding the call anyway would be a guard with nothing
    # behind it, which is the thing this codebase does not do.


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
    if not document.is_current:
        # The route checks is_current before calling here; this is the same
        # check UNDER THE LOCK. A Revise landing between the two leaves a
        # row that is still `sent` -- superseding never changes status --
        # and signing it would record the client agreeing to a version
        # nobody can open, and alert the venue as if they had.
        raise ValueError("a newer version of this document has replaced the one you were sent")

    document.signed_at = dt.datetime.now(dt.timezone.utc)
    document.signer_name = signer_name
    document.signer_ip = signer_ip
    actor = truncate(f"client:{signer_name}", BOOKING_EVENT_ACTOR_MAX_LENGTH)
    document = _transition(db, document, DocumentStatus.signed, actor=actor)

    # An Event Order approval confirms NOTHING about the booking -- the
    # agreement and deposit did that. It records that the client has read
    # the run sheet and accepted it. The venue alert and the client's
    # receipt are NOT sent here: the route schedules
    # deliver_beo_approval_emails to run after the client has their
    # response (Aaron, 2026-09-10: "a client shouldn't wait on our mail
    # server to acknowledge their click").

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


def background_db():
    """A session for work that runs after the response has gone (FastAPI
    BackgroundTasks): by then the request's own session is closed. Tests
    replace this with contextlib.nullcontext(their fixture session), so
    the work lands in the same transaction the test can see."""
    from contextlib import closing

    from app.database import SessionLocal

    return closing(SessionLocal())


RECEIPT_SENT, RECEIPT_NOT_SENT = "beo_approval_receipt_sent", "beo_approval_receipt_not_sent"
ALERT_SENT, ALERT_NOT_SENT = "beo_approved_alert_sent", "beo_approved_alert_not_sent"


def _outcome_event(booking_id: uuid.UUID, not_sent: str | None, *, sent_type: str, not_sent_type: str, actor: str):
    return BookingEvent(
        booking_id=booking_id,
        event_type=sent_type if not_sent is None else not_sent_type,
        new_value=truncate(not_sent, 500) if not_sent else None,
        actor=actor,
    )


def printed_event_date(document: Document) -> str | None:
    """The date THIS version prints ("Friday, 14 May 2027"), which is the
    date the client accepted when they approved it -- not the booking's
    live date, which can have moved since (review, 2026-09-10: a resend
    named a date the client had never seen). None when the version was
    built without a date and prints the [REVIEW] marker instead."""
    timeline = (document.content or {}).get("event_timeline") or {}
    shown = timeline.get("event_date_display")
    if not shown or "[REVIEW]" in shown:
        return None
    return shown


def deliver_beo_approval_emails(document_id: uuid.UUID, *, signer_name: str) -> None:
    """Runs AFTER the client has their 303. The venue alert, then the
    client's one-line receipt (Aaron, 2026-09-10: that their approval and
    the date were received, nothing restated), then a trail row for each
    saying whether it went and why not. Never raises: an exception here
    would surface as a server error for a request that already succeeded.

    Re-reads the row and sends nothing unless it is an approval -- the
    guard for a task scheduled against a row that never signed. A Revise
    landing in the gap does NOT cancel the receipt: the client did approve
    that version, and the receipt names that version's own printed date."""
    from app.services import notifications

    actor = truncate(f"client:{signer_name}", BOOKING_EVENT_ACTOR_MAX_LENGTH)
    try:
        with background_db() as db:
            try:
                document = db.get(Document, document_id)
                if document is None or document.status != DocumentStatus.signed:
                    return
                booking = document.booking
                alert_not_sent = notifications.notify_beo_approved(
                    booking, signer_name=signer_name, version=document.version
                )
                db.add(_outcome_event(booking.id, alert_not_sent, sent_type=ALERT_SENT, not_sent_type=ALERT_NOT_SENT, actor=actor))
                receipt_not_sent = notifications.notify_beo_approval_receipt(
                    booking, version=document.version, event_date_display=printed_event_date(document)
                )
                db.add(
                    _outcome_event(
                        booking.id, receipt_not_sent, sent_type=RECEIPT_SENT, not_sent_type=RECEIPT_NOT_SENT, actor=actor
                    )
                )
                db.commit()
            except Exception:  # noqa: BLE001 -- see docstring
                db.rollback()
                logger.exception("Post-approval alerts or trail failed for document %s", document_id)
    except Exception:  # noqa: BLE001 -- could not even open a session; the approval itself is committed
        logger.exception("Post-approval delivery could not start for document %s", document_id)


def _latest_approved_row(db: Session, booking_id: uuid.UUID):
    rows = version_rows(db, booking_id, DocumentType.beo)
    return next((r for r in rows if r.status == DocumentStatus.signed and not r.is_legacy), None)


def _latest_outcome(db: Session, booking_id: uuid.UUID, *, sent_type: str, not_sent_type: str) -> tuple[str | None, str | None]:
    """("sent" | "not_sent" | None, reason). QUERIED, ordered by created_at,
    the later row winning among equals (a failure and its resend are
    separate requests, so separate transactions, so distinct timestamps in
    production -- Postgres now() is the transaction start; the only tie is
    inside one transaction, where the later insert is the later act).
    "not_sent" only while the latest failure has no send at or after it."""
    rows = db.execute(
        select(BookingEvent.event_type, BookingEvent.new_value, BookingEvent.created_at)
        .where(BookingEvent.booking_id == booking_id, BookingEvent.event_type.in_((sent_type, not_sent_type)))
        .order_by(BookingEvent.seq)
    ).all()
    if not rows:
        return None, None
    last_failure = None
    for r in rows:
        if r.event_type == not_sent_type:
            last_failure = r  # later rows win, ties included
    if last_failure is None:
        return "sent", None
    if any(r.event_type == sent_type and r.created_at >= last_failure.created_at for r in rows):
        return "sent", None
    return "not_sent", last_failure.new_value


def latest_receipt_outcome(db: Session, booking_id: uuid.UUID) -> tuple[str | None, str | None]:
    """The client's approval receipt. An approved Event Order with NO
    receipt row at all is a failure too (review, 2026-09-10): the
    background delivery never finished, and silence is the thing the
    banner exists to end."""
    state, reason = _latest_outcome(db, booking_id, sent_type=RECEIPT_SENT, not_sent_type=RECEIPT_NOT_SENT)
    if state is None and _latest_approved_row(db, booking_id) is not None:
        return "not_sent", "no delivery was recorded for this approval -- the mail step never finished"
    return state, reason


def latest_alert_outcome(db: Session, booking_id: uuid.UUID) -> tuple[str | None, str | None]:
    """The venue's own approval alert -- its failure was silent before."""
    return _latest_outcome(db, booking_id, sent_type=ALERT_SENT, not_sent_type=ALERT_NOT_SENT)


def resend_beo_approval_emails(db: Session, booking: Booking, *, actor: str) -> list[str]:
    """Staff resend from the booking page banner. Sends whichever of the
    two approval emails did not go -- the client's receipt (for the
    highest approved Event Order, naming ITS printed date) and the venue's
    alert -- writes each outcome to the trail under the staff actor, and
    returns the reasons for anything that still failed (empty when all
    went). Refuses, with a reason a person can act on, when nothing can be
    sent: no approved Event Order; both already sent; the contact's email
    is invalid (fix that first -- a click can only append another failure)."""
    from app.services import notifications

    approved = _latest_approved_row(db, booking.id)
    if approved is None:
        raise ValueError("this booking has no approved Event Order, so there is no receipt to send")
    receipt_state, _ = latest_receipt_outcome(db, booking.id)
    alert_state, _ = latest_alert_outcome(db, booking.id)
    if receipt_state == "sent" and alert_state != "not_sent":
        raise ValueError("the approval receipt has already been sent; there is nothing to resend")
    if receipt_state != "sent" and (booking.contact is None or not is_valid_email(booking.contact.email)):
        raise ValueError("the contact's email address is not valid -- fix it on this page first, then resend")

    document = db.get(Document, approved.id)
    failures: list[str] = []
    if receipt_state != "sent":
        not_sent = notifications.notify_beo_approval_receipt(
            booking, version=approved.version, event_date_display=printed_event_date(document)
        )
        db.add(_outcome_event(booking.id, not_sent, sent_type=RECEIPT_SENT, not_sent_type=RECEIPT_NOT_SENT, actor=actor))
        if not_sent:
            failures.append(f"client receipt: {not_sent}")
    if alert_state == "not_sent":
        not_sent = notifications.notify_beo_approved(
            booking, signer_name=document.signer_name or "the client", version=approved.version
        )
        db.add(_outcome_event(booking.id, not_sent, sent_type=ALERT_SENT, not_sent_type=ALERT_NOT_SENT, actor=actor))
        if not_sent:
            failures.append(f"venue alert: {not_sent}")
    db.commit()
    return failures


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
