"""A wizard submission that produced no Event Order reaches Aaron.

THIS FAILURE REMOVES A SURFACE INSTEAD OF ADDING ONE, which is what makes
it different from every other thing that can go wrong here.

`wizard.submit_review` sets the session to `submitted` and COMMITS before
generation runs. Every "ready for the guided wizard" surface excludes a
booking with a submitted session -- `get_wizard_eligible_bookings` ends
`Booking.id.notin_(already_submitted)` -- and that one query feeds the
dashboard count, Triage's wizard-ready list and the digest's own section.

So the moment generation fails: the client sees an error and stops, and
the booking vanishes from every staff worklist it was on. The failure IS
recorded, as a `wizard_generation_failed` BookingEvent, and nothing
anywhere reads it. Reconciliation was named as the only net and did not
have this check.

ASKED OF THE STATE, NOT OF THE EVENT: "submitted, and no Event Order
exists". That catches a crash, a timeout, a deploy mid-request and a draft
somebody later deleted -- not only the failures that got as far as writing
their own trail row. And it clears itself the moment an Event Order
exists, however it arrives.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.models.document import DocumentType
from app.models.wizard_session import WizardSession, WizardSessionStatus
from app.services import digest, documents as documents_service, reconciliation, wizard
from app.services.document_generation import generate_beo_content

CODE = "WIZARD_SUBMITTED_NO_BEO"


def _submitted_session(db, booking):
    session = wizard.get_or_create_session(db, booking, actor="test")
    session.status = WizardSessionStatus.submitted
    db.flush()
    return session


def _codes(db, hamilton):
    return {f.check_code for f in reconciliation.collect(db, hamilton)}


def test_a_submitted_wizard_with_no_event_order_is_found(db, hamilton, booking):
    """THE one."""
    _submitted_session(db, booking)

    assert CODE in _codes(db, hamilton)


def test_the_finding_says_the_booking_has_left_every_worklist(db, hamilton, booking):
    """The detail has to explain why nobody noticed, or it reads as an
    ordinary missing document and gets the ordinary treatment."""
    _submitted_session(db, booking)

    finding = next(f for f in reconciliation.collect(db, hamilton) if f.check_code == CODE)

    assert "dropped off" in finding.detail
    assert "error" in finding.detail


def test_an_event_order_clears_it(db, hamilton, booking):
    """Self-clearing by construction, and this is the positive control: a
    check that fired on every submitted session would pass the probe above
    and flood the digest."""
    _submitted_session(db, booking)
    assert CODE in _codes(db, hamilton)

    documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )

    assert CODE not in _codes(db, hamilton)


def test_a_draft_event_order_counts(db, hamilton, booking):
    """A draft means the run sheet exists and somebody is working on it.
    This check is about the booking that has NONE."""
    _submitted_session(db, booking)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="test"
    )
    assert document.status.value == "draft"

    assert CODE not in _codes(db, hamilton)


def test_an_unsubmitted_wizard_is_not_flagged(db, hamilton, booking):
    """A booking mid-wizard has no Event Order yet and is not a fault --
    it is on the wizard-ready worklist, where it belongs."""
    wizard.get_or_create_session(db, booking, actor="test")
    db.flush()

    assert CODE not in _codes(db, hamilton)


def test_a_booking_with_no_wizard_at_all_is_not_flagged(db, hamilton, booking):
    assert CODE not in _codes(db, hamilton)


def test_it_reaches_the_digest(db, hamilton, booking):
    """Aaron's rule, 2026-09-14: a check that fires without reaching the
    digest is not finished. Driven through run() and the real renderer
    rather than asserted structurally."""
    _submitted_session(db, booking)
    reconciliation.run(db, hamilton)

    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "Wizard Submitted No Beo" in body, (
        "the finding never reached the email -- the heading is derived from the "
        "check code, so this is also asserting the code is registered"
    )
    assert booking.event_name in body


def test_the_check_is_registered(db, hamilton, booking):
    """A check that exists and is never called is a log. collect() is the
    registry, and this is the mistake the register has recorded twice."""
    import inspect

    src = inspect.getsource(reconciliation.collect)
    assert "check_wizard_submitted_without_beo" in src
