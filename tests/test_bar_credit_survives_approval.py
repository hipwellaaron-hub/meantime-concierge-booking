"""The bar credit is a promise, and approving a proposal used to delete it.

A booking's bar credit is generated INTO the Event Order's bar_structure
field -- "$500 bar credit included, applied on the night." -- and printed
nowhere else on the document. That is deliberate: the floor reads the bar
structure on the night, and the credit changes how the tab is run from the
opening drink.

An AI proposal replaces the whole field. Approving one therefore replaced
the credit line along with everything else, and no rule covered it: the
venue's promise to the client vanished from the one place the people who
have to honour it look.

This repair existed on 2bbd23e and went away with the revert of that
commit. Re-done deliberately.
"""

import datetime as dt
from decimal import Decimal

from app.models import Contact
from app.models.document import DocumentType
from app.services import beo_proposals
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import bar_structure_with_credit, generate_beo_content

CREDIT_LINE = "$500 bar credit included, applied on the night."
PROPOSED = "Bar tab to $2,000, then cash bar. House wine and tap beer only."


def _booking_with_credit(db, space, credit=Decimal("500.00")):
    contact = Contact(name="Bar Credit Client", email="bar.credit@example.com")
    db.add(contact)
    db.flush()
    booking = create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Bar Credit Test",
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )
    booking.bar_credit = credit
    db.commit()
    db.refresh(booking)
    return booking


def _beo(db, booking):
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )


# --- the composer ----------------------------------------------------------------


def test_the_generator_puts_the_credit_first_in_the_bar_structure(db, loft):
    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)
    assert document.content["bar_structure"].startswith(CREDIT_LINE)


def test_composing_twice_does_not_double_the_line():
    once = bar_structure_with_credit("Bar tab, $2,000 limit.", Decimal("500"))
    assert once == bar_structure_with_credit(once, Decimal("500"))
    assert once.count("bar credit included") == 1


def test_a_stale_figure_the_model_repeated_is_replaced_with_the_current_one():
    stale = bar_structure_with_credit("Bar tab.", Decimal("300"))
    assert "$300" in stale
    current = bar_structure_with_credit(stale, Decimal("750"))
    assert "$750 bar credit" in current and "$300" not in current
    assert current.endswith("Bar tab."), "the person's own words are untouched"


def test_a_sentence_somebody_wrote_is_never_mistaken_for_the_credit_line():
    """The strip is anchored to the exact wording this module composes.
    A human saying something similar keeps every word."""
    theirs = "Client has $500 credit with us from the deposit -- apply to bar."
    composed = bar_structure_with_credit(theirs, Decimal("500"))
    assert composed.endswith(theirs)


def test_no_credit_means_no_line_and_nothing_stripped_by_mistake():
    assert bar_structure_with_credit("Cash bar.", Decimal("0")) == "Cash bar."
    assert bar_structure_with_credit("Cash bar.", None) == "Cash bar."


# --- through a real approval -----------------------------------------------------


def test_approving_a_bar_structure_proposal_keeps_the_credit(db, loft):
    """The bug, through the real path. The model proposes new bar wording
    with no idea a credit exists; before this, approval wrote its words and
    the promise was gone."""
    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)
    assert CREDIT_LINE in document.content["bar_structure"]

    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"].startswith(CREDIT_LINE), "the promise survives approval"
    assert PROPOSED in document.content["bar_structure"], "and the approved wording is there too"


def test_approving_wording_that_already_carries_the_line_does_not_double_it(db, loft):
    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": CREDIT_LINE + "\n\n" + PROPOSED},
        source="client email", actor="ai:claude",
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"].count("bar credit included") == 1


def test_a_booking_with_no_credit_gains_no_line_on_approval(db, loft):
    booking = _booking_with_credit(db, loft, credit=Decimal("0.00"))
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")

    beo_proposals.approve_field(db, row, actor="staff:aaron@meantime.com.au")

    db.refresh(document)
    assert document.content["bar_structure"] == PROPOSED


# --- the badge still knows who approved the words ------------------------------


def test_the_approval_badge_survives_the_credit_prefix(db, loft):
    """Re-composing the credit on approval made the stored bar_structure
    "credit line + approved words" while applied_value stayed the approved
    words alone, so the regenerate screen's badge -- which compares the two
    -- went quiet on every booking with a credit. A true attribution was
    lost. The credit is generated, not approved, so the badge compares the
    person's part only."""
    from app.services import document_regeneration as dr

    booking = _booking_with_credit(db, loft)
    document = _beo(db, booking)
    proposal, result = beo_proposals.propose(
        db, booking, fields={"bar_structure": PROPOSED}, source="client email", actor="ai:claude"
    )
    assert not result.blocked, result.codes
    row = next(f for f in proposal.fields if f.field == "bar_structure")
    beo_proposals.approve_field(db, row, actor="staff:sally@meantime.com.au")
    db.refresh(document)

    fresh = {**generate_beo_content(booking), "bar_structure": "Cash bar only."}
    loss = next(x for x in dr.losses(db, document, fresh) if x.field == "bar_structure")

    assert loss.approved_note is not None, "Sally approved these words; the credit line is not hers"
    assert "sally@meantime.com.au" in loss.approved_note
