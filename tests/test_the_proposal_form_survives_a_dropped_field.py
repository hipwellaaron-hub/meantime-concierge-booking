"""The Event Order form survives a proposal row for a field that no longer
exists.

review_rows built each text row with `current[field_row.field]` and
`beo_rules.FIELD_LABELS[field_row.field]` -- square brackets on a name
read back out of the database. Drop a field from PROPOSABLE_FIELDS after a
proposal naming it has been stored, and every booking with such a row
pending 500s its Event Order form. The one screen for reviewing proposals,
taken down by the least surprising kind of refactor.

Both are .get now: an unknown field renders under its own name with an
empty "current", and the approval path refuses it separately.

THE PROBE DROPS A REAL FIELD AFTER THE PROPOSAL IS STORED, by patching the
two structures the row builder reads. Proposing an unknown field up front
would be refused at propose time and prove nothing about the form.
"""
from app.models.booking import BookingStatus
from app.services import beo_proposals, beo_rules
from app.services.booking import change_status, create_booking
import datetime as dt


def _confirmed_booking(db, loft, contact, name):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()
    change_status(db, b, BookingStatus.confirmed, actor="test")
    return b


def test_a_row_for_a_field_that_was_dropped_still_renders(db, hamilton, loft, contact, monkeypatch):
    """THE one. The proposal names dietaries; dietaries is then removed
    from the catalogue of fields; the form must still build."""
    booking = _confirmed_booking(db, loft, contact, "ZZDROPPED Field")
    beo_proposals.propose(
        db, booking, fields={"dietaries": "No nuts anywhere"}, source="email", actor="ai:claude"
    )
    assert any(r["field"] == "dietaries" for r in beo_proposals.review_rows(db, booking.id)), (
        "fixture: the row must exist before the field is dropped"
    )

    monkeypatch.setattr(
        beo_rules, "PROPOSABLE_FIELDS", tuple(f for f in beo_rules.PROPOSABLE_FIELDS if f != "dietaries")
    )
    monkeypatch.setattr(
        beo_rules, "FIELD_LABELS", {k: v for k, v in beo_rules.FIELD_LABELS.items() if k != "dietaries"}
    )

    rows = beo_proposals.review_rows(db, booking.id)  # used to KeyError

    row = next(r for r in rows if r["field"] == "dietaries")
    assert row["label"] == "Dietaries", "an unknown field renders under its own name"
    assert row["current"] == ""
    assert row["replaces_text"] is False


def test_a_known_field_still_carries_its_real_label(db, hamilton, loft, contact):
    """The positive control: the fallback must not have replaced the real
    labels."""
    booking = _confirmed_booking(db, loft, contact, "ZZDROPPED Known")
    beo_proposals.propose(
        db, booking, fields={"dietaries": "No nuts anywhere"}, source="email", actor="ai:claude"
    )

    row = next(r for r in beo_proposals.review_rows(db, booking.id) if r["field"] == "dietaries")

    assert row["label"] == beo_rules.FIELD_LABELS["dietaries"]
