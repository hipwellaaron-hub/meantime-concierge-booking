"""Contact dedupe support.

The same person often emails from two addresses, or generates two separate
enquiries for one event. We never auto-merge — two contact rows might
legitimately be different people with similar names. Instead we surface
likely duplicates so a human can confirm the merge.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Contact

NAME_SIMILARITY_THRESHOLD = 85  # 0-100, rapidfuzz token_sort_ratio


@dataclass
class DuplicateCandidate:
    contact: Contact
    reason: str  # "email_match" or "name_similarity"
    score: float  # 100 for exact email match; 0-100 fuzzy score otherwise


def find_duplicate_candidates(
    db: Session, name: str, email: str, *, exclude_contact_id=None
) -> list[DuplicateCandidate]:
    """Return contacts that look like they might be the same person as
    (name, email), ranked most-likely-duplicate first. Does not merge or
    modify anything."""
    candidates: dict = {}  # contact.id -> DuplicateCandidate, keeps best match per contact

    email_matches = db.scalars(
        select(Contact).where(func.lower(Contact.email) == email.strip().lower())
    ).all()
    for contact in email_matches:
        if exclude_contact_id and contact.id == exclude_contact_id:
            continue
        candidates[contact.id] = DuplicateCandidate(contact=contact, reason="email_match", score=100.0)

    all_contacts = db.scalars(select(Contact)).all()
    for contact in all_contacts:
        if exclude_contact_id and contact.id == exclude_contact_id:
            continue
        if contact.id in candidates:
            continue  # already matched on email, that's a stronger signal
        score = fuzz.token_sort_ratio(name.strip().lower(), contact.name.strip().lower())
        if score >= NAME_SIMILARITY_THRESHOLD:
            candidates[contact.id] = DuplicateCandidate(contact=contact, reason="name_similarity", score=score)

    return sorted(candidates.values(), key=lambda c: c.score, reverse=True)


def find_or_create_contact(db: Session, name: str, email: str, phone: str | None) -> tuple[Contact, list[DuplicateCandidate]]:
    """For lead intake: an exact email match is about as certain an
    identity signal as we get, so it's reused rather than spawning a new
    contact row every time the same person enquires again. Anything less
    certain (name-only similarity) is only ever surfaced, never merged --
    see find_duplicate_candidates."""
    existing = db.scalars(select(Contact).where(func.lower(Contact.email) == email.strip().lower())).first()
    if existing is not None:
        return existing, []

    contact = Contact(name=name, email=email, phone=phone)
    db.add(contact)
    db.flush()

    other_candidates = find_duplicate_candidates(db, name, email, exclude_contact_id=contact.id)
    return contact, other_candidates
