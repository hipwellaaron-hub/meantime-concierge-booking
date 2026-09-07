"""The regenerate guard consulting the record instead of only guessing.

losses() decides what a regenerate would destroy. Until now it decided from
the value alone: changed, and not empty, and not a generated placeholder.
That guess is right about most things and wrong about one -- a field a
person deliberately CLEARED looks exactly like a field nobody ever filled
in, so the next regenerate refilled it without a word.

The record is what tells those apart. It is consulted here in one direction
only: it can add a warning, never remove one. The tests below pin both
halves, because the half that is easy to get wrong is the second.
"""

import pathlib

import pytest

from app.models.document import DocumentType
from app.services import content_authorship as ca
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.document_generation import NO_DIETARIES, generate_beo_content

PLACEHOLDER = "[REVIEW] add bar structure"


def _beo(db, booking, overrides):
    content = {**generate_beo_content(booking), **overrides}
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")


def _store(db, document, content):
    document.content = content
    db.commit()
    db.refresh(document)
    return document


def _fresh(booking, **overrides):
    """What a regenerate ACTUALLY hands losses(): content built from the
    booking by the generator, carrying no record of its own.

    Never `{**document.content, ...}`. That was the first version of these
    tests and it hid a real bug -- reading the record off `fresh` instead of
    off the stored content passed every one of them, because a fresh dict
    copied from the document still had the record on it. In production it
    never does (review of commit three)."""
    content = generate_beo_content(booking)
    assert not ca.has_record(content), "the generator does not record authorship"
    return {**content, **overrides}


def _fields(found):
    return sorted(loss.field for loss in found)


# --- what the record adds -----------------------------------------------------


def test_a_field_a_person_cleared_is_no_longer_refilled_in_silence(db, booking):
    """The reason this commit exists. Proved live before it: `music`
    cleared and recorded, the booking still naming a DJ, and losses()
    returned nothing -- so the regenerate put the DJ back and said
    nothing."""
    document = _beo(db, booking, {"music": "DJ till late"})
    _store(db, document, ca.record({**document.content, "music": None}, ["music"]))
    assert ca.authored(document.content) == {"music"}

    found = dr.losses(db, document, _fresh(booking, music="DJ till late"))

    assert _fields(found) == ["music"]
    assert found[0].current == "", "the person's decision was to have nothing there"
    assert found[0].incoming == "DJ till late", "and this is what would go back in"


def test_an_empty_field_nobody_claims_is_still_not_a_loss(db, booking):
    """The other side of the same clause: without a record naming it, an
    empty field is a field nobody has filled in yet, and filling it in is
    the regenerate doing its job. Warning here would be pure noise, and
    noise on this screen is how the original incident happened."""
    document = _beo(db, booking, {"music": None})
    assert not ca.has_record(document.content)

    found = dr.losses(db, document, _fresh(booking, music="DJ till late"))

    assert _fields(found) == []


def test_a_generated_placeholder_is_skipped_even_when_the_record_names_it(db, booking):
    """The secondary skip, and it stays flat rather than becoming a
    question about authorship. A record can name a field whose value has
    since become a placeholder -- nothing calls forget() yet -- and the
    generator's own sentence is still not a person's words."""
    document = _beo(db, booking, {"bar_structure": "Bar tab, $2,000 limit."})
    _store(db, document, ca.record({**document.content, "bar_structure": PLACEHOLDER}, ["bar_structure"]))
    assert ca.authored(document.content) == {"bar_structure"}

    found = dr.losses(db, document, _fresh(booking, bar_structure="Cash bar"))

    assert _fields(found) == [], "there is nothing of anybody's to lose"


# --- what the record must NOT take away ---------------------------------------


def test_a_value_the_record_does_not_name_is_still_reported(db, booking):
    """The load-bearing test of this commit, and the one whose absence
    would recreate Aaron's original incident.

    A record can be PARTIAL. A draft written before the record existed
    carries none; the first hand-edit after it shipped creates one naming
    that single field, and every older human value goes unnamed. Here the
    allergy note was typed before any record existed and the music edit
    created the record -- if silence were read as "the generator wrote it",
    the allergy would be destroyed by the next regenerate, invisibly."""
    document = _beo(db, booking, {"dietaries": "1x severe nut allergy (table 4).", "music": "TBC"})
    assert not ca.has_record(document.content), "as a document written before the record existed"

    # one later hand-edit, which creates a record naming only that field
    _store(db, document, ca.record({**document.content, "music": "DJ till late"}, ["music"]))
    assert ca.authored(document.content) == {"music"}, "the allergy is not named by the record"

    found = dr.losses(db, document, _fresh(booking, dietaries="No dietary requirements declared"))

    assert "dietaries" in _fields(found), "the unnamed allergy is still protected"
    allergy = next(loss for loss in found if loss.field == "dietaries")
    assert allergy.current == "1x severe nut allergy (table 4)."
    assert allergy.incoming == "No dietary requirements declared", "and this is what would replace it"


def test_consulting_the_record_never_removes_a_warning_the_guess_would_give(db, booking):
    """Stated as a property rather than a case: for content with a record
    and content without, every field the old guess would report is still
    reported. The record is additive here, full stop."""
    values = {
        "dietaries": "1x severe nut allergy (table 4).",
        "special_notes": "Rounds of 8.",
        "bar_structure": PLACEHOLDER,
        "music": None,
        "decorations": "Fairy lights, hired.",
    }
    fresh_values = {
        "dietaries": "No dietary requirements declared",
        "special_notes": "Rounds of 10.",
        "bar_structure": "Cash bar",
        "music": "DJ till late",
        "decorations": "Fairy lights, hired.",
    }
    document = _beo(db, booking, values)
    fresh = _fresh(booking, **fresh_values)

    without_record = set(_fields(dr.losses(db, document, fresh)))

    # the same content, now carrying a record that names none of the fields
    # the guess found
    _store(db, document, ca.record(dict(document.content), ["onsite_contact"]))
    with_record = set(_fields(dr.losses(db, document, fresh)))

    assert without_record <= with_record, (
        f"the record removed a warning: {sorted(without_record - with_record)}"
    )
    assert "dietaries" in with_record and "special_notes" in with_record
    assert "bar_structure" not in with_record, "a placeholder is still skipped"


def test_a_document_with_no_record_behaves_exactly_as_before(db, booking):
    """Most documents in the system have no record and will not get one
    until somebody edits them. Their behaviour must be untouched."""
    document = _beo(db, booking, {"dietaries": "1x severe nut allergy (table 4).", "music": None})
    assert not ca.has_record(document.content)

    found = dr.losses(
        db, document, _fresh(booking, dietaries="No dietary requirements declared", music="DJ till late")
    )

    assert _fields(found) == ["dietaries"], "the allergy reported, the empty music field not"


def test_an_unchanged_field_is_never_reported_however_it_is_recorded(db, booking):
    """Cheapest guard on the screen: a regenerate that changes nothing has
    nothing to confirm, and a screen that asks anyway is the screen people
    learn to click through."""
    document = _beo(db, booking, {"dietaries": "1x severe nut allergy (table 4)."})
    _store(db, document, ca.record(dict(document.content), ["dietaries"]))

    found = dr.losses(db, document, _fresh(booking, dietaries="1x severe nut allergy (table 4)."))

    assert _fields(found) == []


# --- the new rows must not become noise, or make claims -----------------------


def test_a_cleared_field_is_not_reported_when_nothing_meaningful_replaces_it(db, booking):
    """Half the warnings this commit first added were nothing-to-nothing: a
    field somebody cleared, with a generated placeholder going back in.
    Worse, ContentLoss.empties_the_field is True for that shape, so the row
    wore the loudest badge on the page. This screen only works while people
    read it (review of 55f39ac)."""
    document = _beo(db, booking, {"bar_structure": "Bar tab, $2,000 limit."})
    _store(db, document, ca.record({**document.content, "bar_structure": None}, ["bar_structure"]))
    assert ca.authored(document.content) == {"bar_structure"}

    found = dr.losses(db, document, _fresh(booking, bar_structure=PLACEHOLDER))

    assert _fields(found) == [], "nothing to nothing is not a decision anybody needs to make"


def test_a_cleared_field_is_still_reported_when_real_words_would_go_back_in(db, booking):
    """The other side of that skip, so quietening the noise does not
    quieten the case the empty-field clause exists for."""
    document = _beo(db, booking, {"bar_structure": "Bar tab, $2,000 limit."})
    _store(db, document, ca.record({**document.content, "bar_structure": None}, ["bar_structure"]))

    found = dr.losses(db, document, _fresh(booking, bar_structure="Cash bar, no tab."))

    assert _fields(found) == ["bar_structure"]


def test_a_cleared_field_is_never_badged_with_somebody_else_s_approval(db, booking):
    """_approval_note matched on the rendered value, so a cleared field --
    which renders as "" -- matched any approved proposal row whose
    applied_value was empty or NULL. The screen then put a gold "approved
    by Sally on 3 Sep 2026" against a blank Sally never approved.

    A wrong value is recoverable. A wrong claim about who authorised it is
    what people rely on when they have stopped checking."""
    import datetime as dt
    from types import SimpleNamespace

    empty_approval = SimpleNamespace(
        applied_value=None,
        decided_by="sally@meantime.com.au",
        decided_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
    )

    assert dr._approval_note([empty_approval], "") is None, "no approval is attributed to a blank"
    assert dr._approval_note([empty_approval], "1x severe nut allergy (table 4).") is None

    real_approval = SimpleNamespace(
        applied_value="1x severe nut allergy (table 4).",
        decided_by="sally@meantime.com.au",
        decided_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
    )
    assert dr._approval_note([real_approval], "1x severe nut allergy (table 4).") == (
        "approved by sally@meantime.com.au on 3 Sep 2026"
    ), "a real approval is still attributed"


# --- how the confirmation screen says it --------------------------------------


def test_a_cleared_field_is_flagged_as_cleared_not_merely_empty(db, booking):
    """The row carries WHY it is empty, rather than leaving a template to
    infer it from a blank string. A statement about what a person did has
    to come from the place that knows they did it."""
    document = _beo(db, booking, {"internal_notes": "Bride requests no seafood."})
    _store(db, document, ca.record({**document.content, "internal_notes": None}, ["internal_notes"]))

    found = dr.losses(db, document, _fresh(booking, internal_notes="Bride requests no seafood."))

    assert _fields(found) == ["internal_notes"]
    assert found[0].cleared_by_a_person is True


def test_a_field_that_still_holds_words_is_not_flagged_as_cleared(db, booking):
    document = _beo(db, booking, {"dietaries": "1x severe nut allergy (table 4)."})

    found = dr.losses(db, document, _fresh(booking, dietaries="No dietary requirements declared"))

    assert _fields(found) == ["dietaries"]
    assert found[0].cleared_by_a_person is False


def test_the_confirmation_screen_says_the_field_was_cleared(db, booking, admin_client):
    """Rendered through the real screen, because the defect this replaces
    was invisible at the service boundary: losses() was right and the page
    showed an unlabelled empty grey box under "what the Event Order says
    now", which reads as a rendering fault rather than somebody's decision
    (review of e7029f2).

    internal_notes is used rather than music because the generator fills it
    from booking.notes -- clearing music produces nothing on either side,
    which is correctly silent and never reaches this screen."""
    import re

    document = _beo(db, booking, {"internal_notes": "Bride requests no seafood."})
    documents_service.update_content(
        db,
        document,
        {**document.content, "internal_notes": None},
        actor="staff:test",
        authored_fields=dr.PROTECTED_FIELD_NAMES,
        placeholders=dr.GENERATED_PLACEHOLDERS,
    )
    db.refresh(document)
    assert "internal_notes" in ca.authored(document.content)

    detail = admin_client.get(f"/admin/bookings/{booking.id}")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 409, "the regenerate stops and asks"
    assert "somebody cleared this field on purpose" in response.text
    assert "Bride requests no seafood." in response.text, "and what would go back in"


# --- the flag is a claim about a person, so it must not rest on a coincidence --


@pytest.mark.parametrize("junk", [{"weird": 1}, [1, 2], 42, True, []])
def test_a_field_holding_the_wrong_type_is_not_called_cleared(db, booking, junk):
    """_render_text returns "" for ANY non-string, so a field holding a
    dict, list, number or bool renders exactly like one somebody emptied.
    Deciding from the rendered string told a staff member that a colleague
    had deliberately cleared a field nobody had touched -- a claim about a
    human action, made from a type coincidence (review of c7ed188).

    JSONB holds whatever was written to it, which is why every read path
    here tolerates the wrong type rather than trusting it."""
    base = generate_beo_content(booking)
    document = _beo(db, booking, {})
    _store(db, document, {**base, "dietaries": junk, ca.AUTHORED_KEY: ["dietaries"]})

    found = dr.losses(db, document, _fresh(booking, dietaries="1x severe nut allergy (table 4)."))

    assert _fields(found) == ["dietaries"], "it is still reported -- something is being replaced"
    assert found[0].cleared_by_a_person is False, "but nobody is said to have cleared it"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_genuinely_blank_recorded_field_is_still_called_cleared(db, booking, blank):
    """The other side: the flag has to keep working for the case it exists
    for, whichever spelling of empty the writer stored."""
    base = generate_beo_content(booking)
    document = _beo(db, booking, {})
    _store(db, document, {**base, "internal_notes": blank, ca.AUTHORED_KEY: ["internal_notes"]})

    found = dr.losses(db, document, _fresh(booking, internal_notes="Bride requests no seafood."))

    assert _fields(found) == ["internal_notes"]
    assert found[0].cleared_by_a_person is True


def test_the_screen_claims_nothing_when_it_cannot_tell(db, booking, admin_client):
    """A row it cannot explain must still not render as a bare empty box,
    and must not invent a reason. It says what it knows: nothing readable."""
    import re

    base = generate_beo_content(booking)
    document = _beo(db, booking, {})
    _store(db, document, {**base, "internal_notes": {"weird": 1}, ca.AUTHORED_KEY: ["internal_notes"]})

    detail = admin_client.get(f"/admin/bookings/{booking.id}")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "nothing readable in this field" in response.text
    assert "somebody cleared this field on purpose" not in response.text


def test_the_no_losses_copy_names_every_skip_the_code_applies(db, booking):
    """Checked against the CONDITIONS, not against my description of them.
    The previous version named three reasons where the code applies four,
    and the commit message describing it was wrong two rounds running."""
    base = generate_beo_content(booking)
    document = _beo(db, booking, {})
    skips = {
        "unchanged": ("same", "same", ["dietaries"]),
        "placeholder current": (NO_DIETARIES, "words", ["dietaries"]),
        "empty, nobody recorded": (None, "words", []),
        "empty, recorded, nothing but a placeholder incoming": (None, NO_DIETARIES, ["dietaries"]),
    }
    for label, (current, incoming, record) in skips.items():
        _store(db, document, {**base, "dietaries": current, ca.AUTHORED_KEY: sorted(record)})
        found = _fields(dr.losses(db, document, _fresh(booking, dietaries=incoming)))
        assert found == [], f"{label} should be skipped, got {found}"

    copy = pathlib.Path("app/templates/admin/regenerate_confirm.html").read_text(encoding="utf-8")
    assert "unchanged" in copy
    assert "generated placeholder" in copy
    assert "nobody recorded as having written it" in copy
    assert "nothing but a placeholder to replace it" in copy, (
        "the fourth skip the code applies must appear in the sentence staff read"
    )
