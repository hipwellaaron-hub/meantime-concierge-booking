"""Document versioning and lifecycle. A new version always supersedes the
previous one rather than overwriting it -- the old row, and the token that
points at it, stay exactly as they were. Every transition is also logged
to booking_events for the same reason booking status changes are: no state
change should require re-reading an email chain to explain.
"""

import datetime as dt
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


def lock_current_for_update(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    """The current document, locked FOR UPDATE.

    For a caller that reads the content, decides something from it, and
    then writes -- regenerating with a human's keep/replace choices is the
    one that exists. Without the lock, an approval committing between the
    read and the write is silently reverted and the audit line still says
    the value was kept (proved live with two sessions, 2026-09-06). The
    same shape update_content_fields already uses for the same reason.
    """
    return db.execute(
        select(Document)
        .where(
            Document.booking_id == booking_id,
            Document.type == doc_type,
            Document.is_current.is_(True),
        )
        .with_for_update()
    ).scalar_one_or_none()


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
) -> Document:
    """`regenerated_note` records what a human chose to keep and what they
    let go when this version replaced one carrying their own words. It is
    written in the SAME transaction as the version: committing the version
    first and the note afterwards would let a crash in between leave the
    values discarded with no record of the decision -- and that record is
    Aaron's stated measure of whether the feature is working."""
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
    db.commit()
    db.refresh(document)

    if doc_type == DocumentType.agreement and previous is not None and previous.status == DocumentStatus.signed:
        # Superseding a signed agreement voids the client's signature. If the
        # booking was confirmed on that signature, a gate is lost: flag it
        # for a human, never move it (see booking.flag_if_confirmed_gate_lost).
        booking_service.flag_if_confirmed_gate_lost(db, booking, actor=actor)
    return document


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

    KNOWN GAP. A caller that built `content` from a read taken before this
    one -- the edit form does, from the GET that rendered it -- may be
    unknowingly reverting a value somebody else committed in between. That
    revert is old last-write-wins behaviour, but recording authorship makes
    it worse: the reverted text is marked as this caller's own words, so a
    regenerate then preserves it. Closing that needs a compare-and-set
    between the page load and the save, which is its own change.
    """
    db.refresh(document, with_for_update=True)
    if document.status != DocumentStatus.draft:
        raise ValueError(f"cannot edit a document that is already {document.status.value} -- only a draft can be edited")

    written = content_authorship.changed_fields(
        document.content, content, candidates=authored_fields, placeholders=placeholders
    )
    # Recorded LAST and assigned, per the module's caller rules: `record`
    # deep-copies, so what it returns is a value SQLAlchemy compares
    # unequal to the one it loaded, and the UPDATE is actually emitted.
    document.content = content_authorship.record(content, written)
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type="document_edited",
            field_name=f"{document.type.value}_version",
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
    content.update(changes)
    document.content = content_authorship.record(content, written)
    db.add(
        BookingEvent(
            booking_id=document.booking_id,
            event_type=event_type,
            field_name=f"{document.type.value}_version",
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
