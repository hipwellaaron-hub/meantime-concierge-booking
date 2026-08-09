from app.models import Contact
from app.services.contact_matching import find_duplicate_candidates


def test_exact_email_match_is_flagged(db):
    existing = Contact(name="Jane Smith", email="jane@example.com", phone="0400000000")
    db.add(existing)
    db.flush()

    candidates = find_duplicate_candidates(db, "Jane S", "JANE@example.com")

    assert len(candidates) == 1
    assert candidates[0].contact.id == existing.id
    assert candidates[0].reason == "email_match"
    assert candidates[0].score == 100.0


def test_similar_name_different_email_is_flagged(db):
    existing = Contact(name="Jonathan Smith", email="jon@work.com", phone=None)
    db.add(existing)
    db.flush()

    candidates = find_duplicate_candidates(db, "Jonathan Smith", "jonathan.smith@gmail.com")

    assert len(candidates) == 1
    assert candidates[0].contact.id == existing.id
    assert candidates[0].reason == "name_similarity"


def test_unrelated_contact_is_not_flagged(db):
    db.add(Contact(name="Bob Jones", email="bob@example.com", phone=None))
    db.flush()

    candidates = find_duplicate_candidates(db, "Alice Wong", "alice@other.com")

    assert candidates == []


def test_no_auto_merge_multiple_contacts_can_coexist(db):
    """Duplicates are surfaced, never merged -- creating a second contact
    that looks like a duplicate must not be blocked or altered."""
    db.add(Contact(name="Jane Smith", email="jane@example.com", phone=None))
    db.flush()

    new_contact = Contact(name="Jane Smith", email="jane.smith@personal.com", phone=None)
    db.add(new_contact)
    db.flush()

    assert new_contact.id is not None
    candidates = find_duplicate_candidates(db, new_contact.name, new_contact.email, exclude_contact_id=new_contact.id)
    assert len(candidates) == 1
