"""The screen that exists to say "tell me before" must check its answer.

A regenerate invalidates every pending proposal: a proposal is reviewed
against a specific version, and approving it afterwards fails with
"replaced by a newer version". So the confirmation screen names the
pending work even when nothing a human wrote is at risk -- Aaron: "If a
regenerate silently invalidates pending work, I will hit exactly that
error without knowing why. Tell me before, not after."

Two holes, both in the write:

  - the confirm returned early when there were no losses, BEFORE the
    compare-and-set ran. The one screen shown purely to warn about pending
    work was the one screen whose answer was never checked;
  - the token itself covered only the losses. Even with losses present, a
    proposal approved, rejected or superseded between the screen and the
    click changed what going ahead would cost, and the token said nothing
    had moved.

So a staff member could be shown one set of outstanding work, click
Regenerate, and destroy a different set, with no second question.

This repair existed on 2bbd23e and went away with the revert of that
commit. It is being re-done deliberately rather than rediscovered.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.beo_proposal import BeoProposalField
from app.models.booking_event import BookingEvent
from app.models.document import DocumentType
from app.services import beo_proposals, documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content

ALLERGY = "1x severe nut allergy (table 4)."
PROPOSED = "2x coeliac, 1x pescatarian."


def _booking(db, space, name):
    contact = Contact(name="Pending Test", email=f"pending.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _beo(db, booking, **overrides):
    content = generate_beo_content(booking)
    content.update(overrides)
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")


def _propose(db, booking, **fields):
    proposal, _ = beo_proposals.propose(
        db, booking, fields=fields, source="email", actor="ai:claude"
    )
    return proposal


def _csrf(client, booking_id):
    page = client.get(f"/admin/bookings/{booking_id}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _shown(client, booking_id, csrf):
    page = client.post(f"/admin/bookings/{booking_id}/documents/beo/generate", data={"csrf_token": csrf})
    assert page.status_code == 409, f"no confirmation screen: {page.status_code}"
    return re.search(r'name="expect" value="([^"]+)"', page.text).group(1), page.text


def _confirm(client, booking_id, csrf, expect, keep=()):
    return client.post(
        f"/admin/bookings/{booking_id}/documents/beo/generate/confirm",
        data={"csrf_token": csrf, "expect": expect, "keep": list(keep)},
    )


def _version(db, booking_id):
    db.expire_all()
    return documents_service.get_current(db, booking_id, DocumentType.beo).version


# --- the screen shown for pending work alone -----------------------------------


def test_a_screen_shown_only_for_pending_work_still_names_it(admin_client, db, loft):
    """The premise. Nothing a human wrote is at risk here -- the document
    holds the generator's placeholders -- so the screen exists for one
    reason, and it has to say so."""
    booking = _booking(db, loft, "Pending Only")
    _beo(db, booking)
    _propose(db, booking, dietaries=PROPOSED)

    _, page = _shown(admin_client, booking.id, _csrf(admin_client, booking.id))
    # The screen names the field, not the proposed text -- the proposal
    # itself is reviewed on the edit page it links to.
    assert "not been reviewed yet" in page
    assert "Dietaries" in page
    assert _version(db, booking.id) == 1, "nothing was written"


def test_the_answer_is_refused_when_the_pending_work_has_moved(admin_client, db, loft):
    """The defect. With no losses the confirm returned before the
    compare-and-set ran, so the answer to this screen was never checked
    against the screen -- and the person is answering "yes, invalidate the
    two fields you showed me" while one of them has since been dealt with
    and something else is now outstanding in its place."""
    booking = _booking(db, loft, "Pending Moved")
    _beo(db, booking)
    proposal = _propose(db, booking, dietaries=PROPOSED, room_layout_notes="Long tables, 3 rows.")

    csrf = _csrf(admin_client, booking.id)
    expect, page = _shown(admin_client, booking.id, csrf)
    # The count wraps across a line in the template, hence the regex.
    assert re.search(r"2 proposed\s+fields", page), "both fields were named"

    # Somebody else deals with one of them while this screen is open.
    rejected = next(row for row in proposal.fields if row.field == "dietaries")
    beo_proposals.reject_field(db, db.get(BeoProposalField, rejected.id), actor="staff:liz")
    db.flush()

    resp = _confirm(admin_client, booking.id, csrf, expect)

    assert resp.status_code == 409, "the stale answer was applied"
    assert re.search(r"1 proposed\s+field on this Event Order", resp.text), (
        "it re-asks about what is outstanding NOW"
    )
    assert _version(db, booking.id) == 1, "a version was written on a stale answer"


def test_when_the_outstanding_work_is_gone_there_is_nothing_left_to_ask(admin_client, db, loft):
    """The branch the refusal above must not swallow. The screen asked one
    question -- may I invalidate this pending work -- and by the time the
    click arrives somebody has rejected it. Nothing is at risk and nothing
    is outstanding, so this is the ordinary one-click regenerate and
    re-asking would show a screen with nothing on it."""
    booking = _booking(db, loft, "Pending Gone")
    _beo(db, booking)
    proposal = _propose(db, booking, dietaries=PROPOSED)

    csrf = _csrf(admin_client, booking.id)
    expect, _ = _shown(admin_client, booking.id, csrf)
    beo_proposals.reject_field(db, db.get(BeoProposalField, proposal.fields[0].id), actor="staff:liz")
    db.flush()

    resp = _confirm(admin_client, booking.id, csrf, expect)

    assert resp.status_code in (200, 303), resp.status_code
    assert _version(db, booking.id) == 2


def test_an_unchanged_answer_still_goes_through(admin_client, db, loft):
    """The other half, and the guard against the two sides of the token
    disagreeing: the screen mints it and the write checks it, so if they
    ever compute it differently every regenerate refuses forever."""
    booking = _booking(db, loft, "Pending Unchanged")
    _beo(db, booking)
    _propose(db, booking, dietaries=PROPOSED)

    csrf = _csrf(admin_client, booking.id)
    expect, _ = _shown(admin_client, booking.id, csrf)

    resp = _confirm(admin_client, booking.id, csrf, expect)

    assert resp.status_code in (200, 303), resp.status_code
    assert _version(db, booking.id) == 2


def test_going_ahead_with_no_losses_still_claims_no_decision(admin_client, db, loft):
    """Nothing was at risk, so no keep decision was made and none is
    recorded -- unchanged by this fix, and pinned so it stays that way."""
    booking = _booking(db, loft, "Pending No Note")
    _beo(db, booking)
    _propose(db, booking, dietaries=PROPOSED)

    csrf = _csrf(admin_client, booking.id)
    expect, _ = _shown(admin_client, booking.id, csrf)
    _confirm(admin_client, booking.id, csrf, expect)

    db.expire_all()
    assert db.query(BookingEvent).filter_by(
        booking_id=booking.id, event_type="document_regenerated"
    ).count() == 0


# --- pending work moving where there ARE losses ---------------------------------


def test_pending_work_moving_refuses_a_decision_about_losses_too(admin_client, db, loft):
    """The token covered only the losses. Rejecting a proposal leaves the
    document untouched, so the losses are identical -- and the old token
    matched, letting a decision made about one set of outstanding work
    destroy a different one."""
    booking = _booking(db, loft, "Pending With Losses")
    _beo(db, booking, dietaries=ALLERGY)
    proposal = _propose(db, booking, room_layout_notes="Long tables, 3 rows.")

    csrf = _csrf(admin_client, booking.id)
    expect, page = _shown(admin_client, booking.id, csrf)
    assert ALLERGY in page, "the loss is on the screen"
    assert "not been reviewed yet" in page and "Room layout notes" in page, "and so is the pending work"

    beo_proposals.reject_field(db, db.get(BeoProposalField, proposal.fields[0].id), actor="staff:liz")
    db.flush()

    resp = _confirm(admin_client, booking.id, csrf, expect, keep=["dietaries"])

    assert resp.status_code == 409, "the losses were unchanged, so the old token matched"
    assert _version(db, booking.id) == 1


def test_a_replacement_proposal_refuses_the_open_screen(admin_client, db, loft):
    """The likelier version of the same thing: a second email arrives and
    supersedes the proposal on the screen. Different field rows, different
    outstanding work, same losses."""
    booking = _booking(db, loft, "Pending Superseded")
    _beo(db, booking, dietaries=ALLERGY)
    _propose(db, booking, room_layout_notes="Long tables, 3 rows.")

    csrf = _csrf(admin_client, booking.id)
    expect, _ = _shown(admin_client, booking.id, csrf)

    _propose(db, booking, room_layout_notes="Rounds of 8, dance floor centre.")
    db.flush()

    resp = _confirm(admin_client, booking.id, csrf, expect, keep=["dietaries"])

    assert resp.status_code == 409
    assert _version(db, booking.id) == 1


# --- the ordinary regenerate is untouched ---------------------------------------


def test_nothing_at_risk_and_nothing_outstanding_is_still_one_click(admin_client, db, loft):
    """No question was asked, so there is no answer to check. This path
    must not have grown a confirmation step."""
    booking = _booking(db, loft, "Pending None")
    _beo(db, booking)

    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate", data={"csrf_token": _csrf(admin_client, booking.id)}
    )

    assert resp.status_code in (200, 303), resp.status_code
    assert _version(db, booking.id) == 2


@pytest.mark.parametrize("row", [{"id": None, "field": "dietaries"}, {}])
def test_a_pending_row_without_an_id_still_fingerprints(row):
    """review_rows builds its own dicts, but the digest must not depend on
    a key being present -- a KeyError here would take out every regenerate
    on a booking with outstanding work."""
    from app.services import document_regeneration

    assert len(document_regeneration.fingerprint([], [row])) == 32
