"""Which fields a save actually changed, and the writers that record it.

Commit 1 built the record; this is the first thing that writes to it. Two
halves: `changed_fields` deciding what a save really changed, and the two
document writers recording that decision.

The load-bearing case is the one that looks like nothing: a staff form
re-posts every field it renders, prefilled and CRLF-encoded, so a naive
writer records all ten free-text fields as hand-written on every save and
freezes the entire Event Order against regeneration. The document would
stop tracking the booking, and nobody would see why.
"""


import pytest

from sqlalchemy import event

from app.models.document import DocumentType
from app.services import content_authorship as ca
from app.services import document_regeneration, documents as documents_service
from app.services.document_generation import NO_DIETARIES, generate_beo_content

PROTECTED = document_regeneration.PROTECTED_FIELD_NAMES
PLACEHOLDERS = document_regeneration.GENERATED_PLACEHOLDERS


# --- what counts as a change --------------------------------------------------


def test_a_crlf_only_difference_is_not_a_change():
    """The reason this commit exists. A browser submits a textarea's line
    breaks as CRLF while the stored JSONB holds LF -- verified, not
    assumed: a real form submission of "line one\\nline two" serialises as
    t=line+one%0D%0Aline+two, CR LF (13, 10).

    Without normalisation every multi-line field differs on every save, so
    every save records every field as a person's words."""
    stored = {"special_notes": "Rounds of 8.\nCake cut at 9."}
    reposted = {"special_notes": "Rounds of 8.\r\nCake cut at 9."}

    assert ca.changed_fields(stored, reposted, candidates=PROTECTED) == set()


def test_a_lone_carriage_return_is_not_a_change_either():
    stored = {"special_notes": "Rounds of 8.\nCake cut at 9."}
    assert ca.changed_fields(stored, {"special_notes": "Rounds of 8.\rCake cut at 9."}, candidates=PROTECTED) == set()


def test_a_real_edit_inside_a_multi_line_field_is_still_a_change():
    """The normalisation must not swallow the edit it is there to find."""
    stored = {"special_notes": "Rounds of 8.\nCake cut at 9."}
    edited = {"special_notes": "Rounds of 8.\r\nCake cut at 9. Nut-free, table 4."}

    assert ca.changed_fields(stored, edited, candidates=PROTECTED) == {"special_notes"}


def test_crlf_inside_a_nested_terms_section_is_not_a_change():
    """terms_sections is the agreement's contract text -- a list of dicts
    whose `body` carries the prose -- so the comparison has to reach it."""
    stored = {"terms_sections": [{"heading": "Minimum spend", "body": "$2,000.\nPayable on the day."}]}
    reposted = {"terms_sections": [{"heading": "Minimum spend", "body": "$2,000.\r\nPayable on the day."}]}

    assert ca.changed_fields(stored, reposted, candidates=PROTECTED) == set()
    assert ca.changed_fields(
        stored,
        {"terms_sections": [{"heading": "Minimum spend", "body": "$3,000.\r\nPayable on the day."}]},
        candidates=PROTECTED,
    ) == {"terms_sections"}


@pytest.mark.parametrize("blank", [None, "", "   ", "\r\n"])
def test_writing_nothing_over_nothing_is_not_a_change(blank):
    """The form writes content["music"] = music.strip() or None on every
    save, over a key that may not have been there. Counting that as a write
    records the person as the author of an empty field, and the regenerate
    then declines to fill in a value they never withheld."""
    assert ca.changed_fields({}, {"music": blank}, candidates=PROTECTED) == set()
    assert ca.changed_fields({"music": None}, {"music": blank}, candidates=PROTECTED) == set()
    assert ca.changed_fields({"music": ""}, {"music": blank}, candidates=PROTECTED) == set()


def test_clearing_a_field_that_had_words_in_it_IS_a_change():
    """Deleting is writing. Somebody made that decision, and the record
    says so rather than leaving the empty value looking generated."""
    assert ca.changed_fields({"music": "DJ till late"}, {"music": ""}, candidates=PROTECTED) == {"music"}


def test_a_key_the_stored_content_never_had_counts_when_it_has_words():
    assert ca.changed_fields({}, {"dietaries": "1x severe nut allergy"}, candidates=PROTECTED) == {"dietaries"}


def test_only_the_named_candidates_are_ever_returned():
    """The whole reason candidates is required. vendors and event_timeline
    change constantly and are machine-derived; recording them would freeze
    the very content a regenerate exists to rebuild."""
    stored = {"vendors": [{"name": "Old Florist"}], "dietaries": "none"}
    incoming = {"vendors": [{"name": "New Florist"}], "dietaries": "1x severe nut allergy"}

    assert ca.changed_fields(stored, incoming, candidates=PROTECTED) == {"dietaries"}
    assert ca.changed_fields(stored, incoming, candidates=()) == set()


def test_keys_absent_from_the_incoming_dict_are_not_considered():
    """A partial `changes` dict says nothing about the keys it omits."""
    stored = {"dietaries": "1x severe nut allergy", "music": "DJ"}
    assert ca.changed_fields(stored, {"music": "Band"}, candidates=PROTECTED) == {"music"}


def test_stored_content_that_is_not_a_dict_is_read_as_holding_nothing():
    """A malformed row must not be the thing that breaks a save."""
    assert ca.changed_fields(None, {"dietaries": "allergy"}, candidates=PROTECTED) == {"dietaries"}


def test_incoming_content_that_is_not_a_dict_is_refused():
    with pytest.raises(TypeError, match="must be a dict"):
        ca.changed_fields({}, ["dietaries"], candidates=PROTECTED)


# --- the generator's own words are nobody's ------------------------------------


def test_a_field_set_to_a_generated_placeholder_is_never_recorded():
    """The staff form writes content["dietaries"] = dietaries.strip() or
    NO_DIETARIES, so clearing the box substitutes the GENERATOR's
    placeholder. Without this the record claims a person wrote "No dietary
    requirements declared" and a later regenerate preserves the placeholder
    as theirs -- the field stops tracking the booking while displaying text
    nobody typed (review of b1e3f4e)."""
    stored = {"dietaries": "1x severe nut allergy (table 4)."}
    blanked = {"dietaries": NO_DIETARIES}

    assert ca.changed_fields(stored, blanked, candidates=PROTECTED) == {"dietaries"}, "changed, without the rule"
    assert ca.changed_fields(stored, blanked, candidates=PROTECTED, placeholders=PLACEHOLDERS) == set()


@pytest.mark.parametrize("placeholder", sorted(document_regeneration.GENERATED_PLACEHOLDERS))
def test_every_generated_placeholder_is_treated_the_same_way(placeholder):
    """Pinned across the whole set rather than the one the review found, so
    a placeholder added to the generator later is covered by construction."""
    assert ca.changed_fields(
        {"special_notes": "Rounds of 8."},
        {"special_notes": placeholder},
        candidates=("special_notes",),
        placeholders=PLACEHOLDERS,
    ) == set()


def test_clearing_a_placeholder_is_not_a_write_either():
    """A placeholder reads as nothing on BOTH sides. Staff who empty a
    "[REVIEW] add bar structure" box have not written anything -- and if
    that were recorded, the regenerate would stop filling in the very value
    the placeholder was asking for. Found by an end-to-end save that
    recorded four fields nobody had touched."""
    stored = {"bar_structure": "[REVIEW] add bar structure"}

    assert ca.changed_fields(stored, {"bar_structure": ""}, candidates=PROTECTED, placeholders=PLACEHOLDERS) == set()
    assert ca.changed_fields(stored, {"bar_structure": None}, candidates=PROTECTED, placeholders=PLACEHOLDERS) == set()
    assert ca.changed_fields(
        stored, {"bar_structure": "Bar tab, $2,000 limit"}, candidates=PROTECTED, placeholders=PLACEHOLDERS
    ) == {"bar_structure"}, "but filling one in certainly is"


def test_a_human_sentence_that_merely_contains_the_review_marker_is_still_recorded():
    """Matched exactly, never as a substring -- the same rule the
    regenerate guard already follows. "Client bringing cake. [REVIEW]
    confirm nut-free with kitchen" is a person's sentence carrying an
    allergy follow-up, not a placeholder."""
    written = "Client bringing cake. [REVIEW] confirm nut-free with kitchen"
    assert ca.changed_fields(
        {"special_notes": "Rounds of 8."},
        {"special_notes": written},
        candidates=PROTECTED,
        placeholders=PLACEHOLDERS,
    ) == {"special_notes"}


def test_placeholders_given_as_a_bare_string_are_refused():
    """The same typo guard `candidates` has: a string is iterable, so a
    single placeholder passed bare would silently become a set of its
    characters and match nothing."""
    with pytest.raises(TypeError, match="not a single string"):
        ca.changed_fields({}, {"dietaries": "x"}, candidates=PROTECTED, placeholders=NO_DIETARIES)


@pytest.mark.parametrize("empty", [[], {}])
def test_an_empty_container_is_nothing_written_just_as_an_empty_string_is(empty):
    """The blank rule was true of strings only, so absent-vs-[] counted as
    a write while absent-vs-"" did not. Unreachable today -- terms_sections
    is the only container in the protected set and the form rejects an
    empty one -- and live the moment a container-valued field joins it."""
    assert ca.changed_fields({}, {"terms_sections": empty}, candidates=PROTECTED) == set()
    assert ca.changed_fields({"terms_sections": None}, {"terms_sections": empty}, candidates=PROTECTED) == set()


# --- the writers ---------------------------------------------------------------


def _draft_beo(db, booking, content):
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")


def test_a_staff_edit_records_only_the_field_it_changed(db, booking):
    """The form posts all ten free-text fields on every save. Only the one
    that moved is a person's words."""
    document = _draft_beo(db, booking, {"dietaries": "No dietary requirements declared", "music": "TBC"})

    documents_service.update_content(
        db,
        document,
        {"dietaries": "1x severe nut allergy (table 4).", "music": "TBC"},
        actor="staff:test",
        authored_fields=PROTECTED,
    )

    assert ca.authored(document.content) == {"dietaries"}, "music was re-posted unchanged, not written"


def test_re_saving_the_same_form_records_nothing_and_starts_no_record(db, booking):
    """Opening the edit screen and pressing save changes nothing, so it
    must say nothing -- and in particular must not stamp a document that
    predates the record with 'recorded, and nobody wrote anything', which
    would tell the next regenerate every field is free to overwrite."""
    stored = {"dietaries": "1x severe nut allergy (table 4).", "special_notes": "Rounds of 8.\nCake at 9."}
    document = _draft_beo(db, booking, dict(stored))
    assert not ca.has_record(document.content)

    documents_service.update_content(
        db,
        document,
        {"dietaries": "1x severe nut allergy (table 4).", "special_notes": "Rounds of 8.\r\nCake at 9."},
        actor="staff:test",
        authored_fields=PROTECTED,
    )

    assert ca.has_record(document.content) is False, "still unknowable, not 'nobody wrote it'"
    assert document.content["special_notes"] == "Rounds of 8.\r\nCake at 9.", "the posted value is stored verbatim"


def test_refreshing_a_machine_derived_snapshot_records_nobody(db, booking):
    """The vendor bump-in confirmation rewrites `vendors` and
    `event_timeline` on the draft. Those are rebuilt from the booking every
    regenerate, and recording them as somebody's words would stop that."""
    document = _draft_beo(db, booking, {"vendors": [{"name": "Old Florist"}], "dietaries": "none"})

    documents_service.update_content(
        db,
        document,
        {"vendors": [{"name": "New Florist"}], "dietaries": "none"},
        actor="staff:test",
    )

    assert ca.has_record(document.content) is False
    assert document.content["vendors"] == [{"name": "New Florist"}], "but the refresh itself still lands"


def test_a_partial_write_does_not_erase_authorship_it_cannot_see(db, booking):
    """Caller rule 1, proved through the writer. update_content_fields is
    handed a `changes` dict holding only the keys being written; recording
    against that fragment would produce a record naming only those keys and
    erase every other name -- here, an allergy note recorded by the
    previous save would stop being a person's words."""
    document = _draft_beo(db, booking, {"dietaries": "none", "music": "TBC", "decorations": "TBC"})

    documents_service.update_content_fields(
        db, document, {"dietaries": "1x severe nut allergy (table 4)."}, actor="staff:test", authored_fields=PROTECTED
    )
    assert ca.authored(document.content) == {"dietaries"}

    documents_service.update_content_fields(
        db, document, {"music": "DJ till late"}, actor="staff:test", authored_fields=PROTECTED
    )

    assert ca.authored(document.content) == {"dietaries", "music"}, "the allergy is still a person's words"
    assert document.content["decorations"] == "TBC", "and the untouched field is untouched"


def test_approving_a_proposal_records_the_field_as_a_persons_words(db, booking):
    """An approval is somebody reading a proposed value and choosing to
    put it on the document. The event type stays beo_proposal_applied --
    the regenerate screen reads document_edited as 'somebody typed here' --
    but the words are a person's either way."""
    document = _draft_beo(db, booking, {"dietaries": "No dietary requirements declared"})

    documents_service.update_content_fields(
        db,
        document,
        {"dietaries": "1x severe nut allergy (table 4)."},
        actor="staff:test",
        event_type="beo_proposal_applied",
        authored_fields=PROTECTED,
    )

    assert ca.authored(document.content) == {"dietaries"}


def test_the_record_is_in_the_update_actually_sent_to_the_row(db, booking):
    """document.content is JSONB with no mutation tracking, and SQLAlchemy
    decides whether to emit an UPDATE by comparing against the value it
    loaded. If the writer hands back something equal to that, no statement
    is sent and the record lives in memory until the next regenerate
    destroys the value it was meant to protect.

    Asserted against the statement actually executed. An earlier version of
    this test expired the session and re-read, which proves less than it
    looks: update_content already calls db.refresh() before returning, so
    the value was DB-sourced either way (review of b1e3f4e)."""
    document = _draft_beo(db, booking, {"dietaries": "none"})
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        # Not `"UPDATE" in statement` -- SELECT ... FOR UPDATE matches that,
        # and a filter that catches the lock instead of the write would pass
        # while proving nothing.
        if statement.strip().upper().startswith("UPDATE") and "documents" in statement.lower():
            captured.append(parameters)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", capture)
    try:
        documents_service.update_content(
            db,
            document,
            {"dietaries": "1x severe nut allergy (table 4)."},
            actor="staff:test",
            authored_fields=PROTECTED,
        )
    finally:
        event.remove(bind, "after_cursor_execute", capture)

    assert captured, "no UPDATE on documents was emitted at all"
    written = [
        getattr(params["content"], "obj", params["content"])
        for params in captured
        if isinstance(params, dict) and "content" in params
    ]
    assert any(isinstance(d, dict) and d.get(ca.AUTHORED_KEY) == ["dietaries"] for d in written), (
        f"an UPDATE was emitted but the record was not in its payload: {written}"
    )
