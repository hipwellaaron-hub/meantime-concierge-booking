"""Document versioning and lifecycle. A new version always supersedes the
previous one rather than overwriting it -- the old row, and the token that
points at it, stay exactly as they were. Every transition is also logged
to booking_events for the same reason booking status changes are: no state
change should require re-reading an email chain to explain.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, BookingEvent, Document
from app.models.document import DocumentStatus, DocumentType
from app.utils import is_valid_email, truncate

BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def get_current(db: Session, booking_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    return db.execute(
        select(Document).where(
            Document.booking_id == booking_id,
            Document.type == doc_type,
            Document.is_current.is_(True),
        )
    ).scalar_one_or_none()


def get_by_token(db: Session, token: str) -> Document | None:
    return db.execute(select(Document).where(Document.access_token == token)).scalar_one_or_none()


def create_new_version(db: Session, booking: Booking, doc_type: DocumentType, content: dict, *, actor: str) -> Document:
    if booking.parent_booking_id is not None:
        # A linked child (see app.services.booking.add_linked_space) is
        # just a second room for the parent's event -- its own documents
        # would duplicate the parent's, not describe anything real.
        raise ValueError("cannot create a document on a linked booking -- use the parent booking instead")
    previous = get_current(db, booking.id, doc_type)
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
    return _transition(db, document, DocumentStatus.sent, actor=actor)


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
    return _transition(db, document, DocumentStatus.signed, actor=actor)
