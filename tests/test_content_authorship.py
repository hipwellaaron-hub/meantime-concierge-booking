"""The record of which document fields a person wrote.

Pure functions over a content dict -- no database, because the record is
just data. What consumes it comes later; these pin the record's own
properties, and in particular the ones the first attempt got wrong and the
ones the reviews of this commit caught.

Two of them pin a HAZARD rather than a guarantee: recording against a
fragment, and editing the loaded object before recording. Neither can be
fixed inside this module -- both are the caller's order of operations --
so the tests state what actually happens, and the module docstring's two
caller rules are written against them. Commit 2 is the first caller, and
these are what it has to be composed around.
"""

import copy
import json

import pytest

from app.services import content_authorship as ca


def test_nothing_is_authored_until_something_is_recorded():
    assert ca.authored({"dietaries": "x"}) == set()
    assert ca.authored({}) == set()
    assert ca.authored(None) == set()


def test_recording_a_write_names_exactly_that_field():
    content = ca.record({"dietaries": "1x severe nut allergy (table 4).", "music": "DJ."}, ["dietaries"])
    assert ca.authored(content) == {"dietaries"}


def test_any_writer_can_start_the_record():
    """The property the first attempt lacked. It recorded what the
    GENERATOR produced, so only the generator could create the record and
    a document written before the feature could never acquire one -- it
    stayed permanently 'unknowable'. Here the first person to write to
    such a document records exactly what they wrote."""
    legacy = {"dietaries": "No dietary requirements declared", "special_notes": "Rounds of 8."}
    assert not ca.has_record(legacy)

    recorded = ca.record(legacy, ["special_notes"])

    assert ca.has_record(recorded)
    assert ca.authored(recorded) == {"special_notes"}, "and only that field"


def test_an_absent_record_is_distinguishable_from_an_empty_one():
    """These are different situations -- content something recorded
    nothing on versus content that predates the record -- and they send a
    reader in opposite directions, so the difference has to survive.

    The empty record is written literally here because no function in this
    module will create one from nothing; see the test below."""
    fresh = {"music": "DJ.", ca.AUTHORED_KEY: []}
    legacy = {"music": "DJ."}

    assert ca.authored(fresh) == ca.authored(legacy) == set()
    assert ca.has_record(fresh) is True
    assert ca.has_record(legacy) is False


# --- the functions are pure: SQLAlchemy will not see an in-place change ------


def test_recording_returns_a_new_dict_and_leaves_the_original_alone():
    """document.content is a JSONB column, and SQLAlchemy decides whether
    to emit an UPDATE by comparing the attribute against the value it
    loaded -- by EQUALITY. A dict mutated in place is the same object and
    so trivially equal, the write is silently dropped, and the record
    lives in memory only until the next regenerate destroys the value it
    was meant to protect. An earlier draft of this module mutated and
    recommended exactly that pattern (review of 686633a)."""
    original = {"dietaries": "1x severe nut allergy (table 4)."}

    recorded = ca.record(original, ["dietaries"])

    assert recorded is not original, "a new object, or the UPDATE is never emitted"
    assert ca.AUTHORED_KEY not in original, "the caller's dict is untouched"
    assert ca.authored(recorded) == {"dietaries"}


def test_forgetting_returns_a_new_dict_too():
    original = ca.record({"music": "DJ."}, ["music"])
    forgotten = ca.forget(original, ["music"])
    assert forgotten is not original
    assert ca.authored(original) == {"music"}, "the caller's dict is untouched"
    assert ca.authored(forgotten) == set()


# --- additive, never subtractive ---------------------------------------------


def test_recording_never_drops_a_name_for_a_field_the_dict_no_longer_holds():
    """The trap an earlier draft fell into: reading the existing record
    THROUGH an intersection with the caller's dict made record()
    subtractive, so a name whose field was absent was silently erased.

    Note what this dict IS -- content with a key removed but the record
    still on it. That is NOT the shape of update_content_fields' `changes`;
    see the fragment test below, which is a different and worse problem
    that this test was previously mistaken for covering."""
    full = ca.record({"dietaries": "allergy", "music": "DJ."}, ["dietaries"])

    missing_a_field = {k: v for k, v in full.items() if k != "dietaries"}
    assert ca.AUTHORED_KEY in missing_a_field, "the record itself is still here"

    updated = ca.record(missing_a_field, ["music"])

    assert ca.authored(updated) == {"dietaries", "music"}, "the unseen name survived"


def test_recording_against_a_fragment_records_only_the_fragment():
    """HAZARD, not a guarantee -- and the reason for caller rule 1.

    update_content_fields is handed a `changes` dict holding only the keys
    being written. It carries no record, because the record lives on the
    stored content. So recording against it produces a record naming only
    those keys, and merging that over the stored content erases every
    other name -- here a declared allergy stops being a person's words and
    becomes the generator's to overwrite.

    This module cannot detect it: a fragment and a whole document are the
    same type, and 'no record present' is a legitimate state, because that
    is what a legacy document looks like. Merging first is the caller's
    job, so both orders are pinned here and commit 2 is written against
    the right one."""
    stored = ca.record(
        {"dietaries": "1x severe nut allergy (table 4).", "special_notes": "Rounds of 8.", "music": "DJ."},
        ["dietaries", "special_notes"],
    )
    changes = {"music": "Band from 8pm."}
    assert not ca.has_record(changes), "a fragment never carries the record"

    wrong = {**stored, **ca.record(changes, ["music"])}
    assert ca.authored(wrong) == {"music"}, "the allergy's authorship is gone"

    right = ca.record({**stored, **changes}, ["music"])
    assert ca.authored(right) == {"dietaries", "special_notes", "music"}


def test_a_name_survives_its_field_going_missing_and_coming_back():
    content = ca.record({"dietaries": "allergy", "music": "DJ."}, ["dietaries", "music"])
    without = {k: v for k, v in content.items() if k != "music"}
    assert ca.authored(without) == {"dietaries", "music"}

    restored = ca.record({**without, "music": "DJ."}, [])
    assert ca.authored(restored) == {"dietaries", "music"}


def test_recording_is_cumulative_and_idempotent():
    content = {"dietaries": "a", "music": "b", "decorations": "c"}
    content = ca.record(content, ["dietaries"])
    content = ca.record(content, ["music"])
    content = ca.record(content, ["music"])
    assert ca.authored(content) == {"dietaries", "music"}


def test_forgetting_hands_a_field_back_to_the_generator():
    content = ca.record({"dietaries": "a", "music": "b"}, ["dietaries", "music"])
    assert ca.authored(ca.forget(content, ["music"])) == {"dietaries"}


def test_forgetting_on_content_with_no_record_starts_no_record():
    """Forgetting is not a write; it must not conjure a record that then
    reads as 'a person wrote nothing here'."""
    assert not ca.has_record(ca.forget({"dietaries": "a"}, ["dietaries"]))


# --- no function here turns "no record" into an empty one ---------------------


@pytest.mark.parametrize("nothing_usable", [[], ["_reference"], [ca.AUTHORED_KEY], ["_a", "_b"]])
def test_recording_nothing_on_a_document_with_no_record_conjures_no_record(nothing_usable):
    """The reversal that got the first attempt reverted, reached through
    the front door. record() used to write _authored unconditionally, so a
    call with no usable name -- an empty `changes` dict is enough -- turned
    a pre-feature document from "nobody has ever recorded here, go and read
    the audit trail" into "recorded, and nobody wrote anything, help
    yourself". Every field on it, including a typed allergy note, then read
    as the generator's to overwrite.

    update_content_fields has no empty-changes guard at its only caller, so
    this was one line of commit 2 away from being live."""
    legacy = {"dietaries": "1x severe nut allergy (table 4)."}
    assert not ca.has_record(legacy)

    unchanged = ca.record(legacy, nothing_usable)

    assert ca.has_record(unchanged) is False, "still unknowable"
    assert unchanged == legacy, "and the content is otherwise exactly what it was"
    assert unchanged is not legacy, "though still never the caller's own object"


@pytest.mark.parametrize("nothing_usable", [[], ["_reference"]])
def test_recording_nothing_on_a_document_that_has_a_record_keeps_it(nothing_usable):
    """The other side: where a record already exists there is nothing to
    conjure, and a no-op record must leave it exactly as it stands -- EQUAL
    to the loaded value, so the row is not rewritten."""
    loaded = ca.record({"dietaries": "allergy", "music": "DJ."}, ["dietaries"])

    unchanged = ca.record(loaded, nothing_usable)

    assert ca.authored(unchanged) == {"dietaries"}
    assert unchanged == loaded, "a no-op save does not rewrite the row"


def test_recording_nothing_still_rewrites_a_record_that_was_carrying_junk():
    """What separates "skip when there is nothing to add" from "skip
    whenever there is nothing to add": where a record EXISTS, the write
    still goes through, and that is what cleans an unusable member out
    instead of re-reading and re-filtering it on every load for ever.

    Only where there is no record at all is the write skipped entirely,
    and there the reason is that writing would conjure one."""
    loaded = {"dietaries": "allergy", ca.AUTHORED_KEY: ["dietaries", 42, ""]}

    unchanged = ca.record(loaded, [])

    assert ca.authored(unchanged) == {"dietaries"}
    assert unchanged[ca.AUTHORED_KEY] == ["dietaries"], "the junk was cleaned out"


def test_an_empty_record_still_carries_as_an_empty_record():
    """Not conjuring is not the same as dropping. Content something
    recorded nothing on keeps saying so across a rebuild, because
    "nothing here is a person's" and "nobody has ever looked" are the two
    answers this module exists to keep apart."""
    previous = {"dietaries": "generated", ca.AUTHORED_KEY: []}

    carried = ca.carry({"dietaries": "regenerated"}, previous=previous)

    assert ca.has_record(carried) is True
    assert ca.authored(carried) == set()


# --- carry cannot be called with its arguments the wrong way round ------------


def test_carry_refuses_the_positional_order_that_would_discard_the_rebuild():
    """Both arguments are content dicts, so a swap raises nothing and
    quietly returns the PREVIOUS version's text as the rebuild's --
    discarding the regenerate while every log line says it worked. The
    keyword makes that order unspellable rather than merely discouraged."""
    previous = ca.record({"dietaries": "allergy", "timeline": "OLD"}, ["dietaries"])
    fresh = {"dietaries": "No dietary requirements declared", "timeline": "REBUILT"}

    with pytest.raises(TypeError):
        ca.carry(previous, fresh)                       # the swap

    assert ca.carry(fresh, previous=previous)["timeline"] == "REBUILT"


def test_carry_keeps_any_record_the_fresh_content_already_had():
    """carry moves the previous record ONTO fresh, it does not replace
    fresh's own. Replacing would fail in the destructive direction: a name
    recorded on the rebuilt content -- by whatever built it, or by a write
    between the rebuild and the carry -- would be dropped, and the field it
    named would stop being a person's."""
    previous = ca.record({"dietaries": "allergy"}, ["dietaries"])
    fresh = ca.record({"dietaries": "regenerated", "music": "DJ."}, ["music"])

    carried = ca.carry(fresh, previous=previous)

    assert ca.authored(carried) == {"dietaries", "music"}


# --- the caller's order of operations ----------------------------------------


def test_editing_the_loaded_object_first_is_the_order_that_loses_the_edit():
    """HAZARD, and the reason for caller rule 2.

    The deep copy protects a nested value edited on the RESULT. It cannot
    protect one edited on the loaded object BEFORE the call: by then the
    value SQLAlchemy compares against already holds the new text, the
    result compares equal to it, and no UPDATE is emitted.

    The codebase's own idiom -- take the content, edit it, assign it back
    -- is exactly this order, so a caller written without rule 2 in front
    of it will compose it this way and lose a hand-negotiated clause."""
    loaded = ca.record({"terms_sections": [{"heading": "Minimum spend", "body": "$2,000."}]}, ["terms_sections"])
    as_loaded = copy.deepcopy(loaded)

    loaded["terms_sections"][0]["body"] = "$3,000, hand-negotiated."   # the wrong order
    result = ca.record(loaded, ["terms_sections"])

    assert loaded != as_loaded, "the loaded object itself now holds the new text"
    assert result == loaded, "so the result compares equal and the UPDATE is never emitted"


# --- what counts as a name, and as a record ----------------------------------


def test_metadata_keys_are_never_authorship():
    """Including the record's own key -- it must not describe itself."""
    content = ca.record({"dietaries": "a", "_reference": {"x": 1}}, ["dietaries", "_reference", ca.AUTHORED_KEY])
    assert ca.authored(content) == {"dietaries"}
    assert content[ca.AUTHORED_KEY] == ["dietaries"], "and the STORED list is clean, not just the read"


@pytest.mark.parametrize("bad", ["dietaries", b"dietaries"])
def test_a_bare_string_of_field_names_is_refused(bad):
    """A string is iterable, so record(content, "dietaries") would record
    seven letters and no field -- and leave a record behind that reads as
    'authorship was recorded and nobody wrote anything'. That is the
    likeliest typo at any call site."""
    expected = f"not a single {type(bad).__name__}"
    with pytest.raises(TypeError, match=expected):
        ca.record({"dietaries": "a"}, bad)
    with pytest.raises(TypeError, match=expected):
        ca.forget(ca.record({"dietaries": "a"}, ["dietaries"]), bad)


@pytest.mark.parametrize("not_a_collection", [42, None, 3.5, object()])
def test_keys_that_are_not_a_collection_at_all_are_refused(not_a_collection):
    """A non-iterable escaped both guards before: it is not a string, so
    the first guard passed it, and the set comprehension then raised a
    bare 'object is not iterable' naming neither the argument nor the
    function -- from a line the caller never wrote."""
    with pytest.raises(TypeError, match="collection of field names"):
        ca.record({"dietaries": "a"}, not_a_collection)
    with pytest.raises(TypeError, match="collection of field names"):
        ca.forget(ca.record({"dietaries": "a"}, ["dietaries"]), not_a_collection)


def test_both_writers_check_their_arguments_in_the_same_order():
    """record and forget validated in opposite orders, so the same bad
    call reported two different faults depending which one you rang."""
    with pytest.raises(TypeError, match="must be a dict"):
        ca.record(None, 42)
    with pytest.raises(TypeError, match="must be a dict"):
        ca.forget(None, 42)


@pytest.mark.parametrize("junk", [
    "dietaries",                       # a string, not a list
    42,
    {"dietaries": True},
    None,
    [42, None, {"x": 1}],              # a LIST of junk -- the shape the review found
    ["_authored", "_reference"],       # a list of metadata names only
])
def test_a_malformed_record_reads_as_no_record_at_all(junk):
    """Content is data from a JSONB column. A value written by an older or
    foreign writer must read as 'nothing was recorded here', never as
    'authorship was recorded and nobody wrote anything' -- those lead to
    opposite decisions about whether a field may be overwritten."""
    content = {"dietaries": "a", ca.AUTHORED_KEY: junk}
    assert ca.authored(content) == set()
    assert ca.has_record(content) is False


@pytest.mark.parametrize("not_a_dict", ["abc", ["dietaries"], 42, 0, ""])
def test_content_that_is_not_a_dict_degrades_instead_of_raising(not_a_dict):
    """A JSONB column holds any JSON value. A row written as a list or a
    string must not be the thing that 500s a document page."""
    assert ca.authored(not_a_dict) == set()
    assert ca.has_record(not_a_dict) is False


def test_the_record_survives_the_json_round_trip_it_is_stored_through():
    """content is a JSONB column, so the record has to be plain JSON."""
    content = ca.record({"dietaries": "a", "music": "b"}, ["music", "dietaries"])
    round_tripped = json.loads(json.dumps(content))
    assert ca.authored(round_tripped) == {"dietaries", "music"}
    assert ca.has_record(round_tripped)


def test_the_stored_list_is_sorted_by_every_writer():
    """Sortedness is load-bearing: it is what makes a re-record idempotent
    BY EQUALITY, and equality is what decides whether SQLAlchemy rewrites
    the row.

    Asserted against an explicit sorted() and against a second
    construction order, because a small set orders by luck: `sorted ->
    list` survived on half of all hash seeds in record() (review of
    b722d81), and in forget() it survived on every seed tried, because
    forget had no order assertion behind it at all (review of 96d1173)."""
    names = ["special_notes", "dietaries", "music", "accessibility"]

    recorded = ca.record({n: "v" for n in names}, names)
    assert recorded[ca.AUTHORED_KEY] == sorted(names)

    kept = sorted(set(names) - {"music"})
    forgotten = ca.forget(recorded, ["music"])
    assert forgotten[ca.AUTHORED_KEY] == kept

    # The same three names reached by a different route. Set iteration
    # order depends on insertion history as well as on hashes, so an
    # unsorted implementation gives these two different lists even on a
    # seed where each happens to come out sorted on its own.
    other_route = ca.record({n: "v" for n in names}, ["music", "accessibility"])
    other_route = ca.record(other_route, ["dietaries", "special_notes"])
    other_route = ca.forget(other_route, ["music"])
    assert other_route[ca.AUTHORED_KEY] == kept
    assert other_route == forgotten, "so the two compare equal, and neither rewrites the row"


@pytest.mark.parametrize("unwritable", [("dietaries",), {"dietaries"}, frozenset({"dietaries"})])
def test_only_a_list_is_read_as_a_record(unwritable):
    """A list is what JSONB returns and the only shape record() writes. A
    set cannot be stored at all -- psycopg raises at flush, far from the
    line that built it -- and reading any of these would invite a caller
    to hand-build content['_authored'] = {...} and hit that. Refusing
    fails in the safe direction: 'no record' sends a reader to the audit
    trail, where 'recorded, nobody wrote anything' invites an overwrite."""
    content = {"dietaries": "a", ca.AUTHORED_KEY: unwritable}
    assert ca.authored(content) == set()
    assert ca.has_record(content) is False


# --- the boundaries the re-reviews found unpinned -----------------------------


def test_the_copy_is_deep_so_a_caller_cannot_edit_the_loaded_object():
    """A shallow copy shares the nested values, so editing
    content["terms_sections"][0]["body"] on the "copy" edits the loaded
    object too -- and SQLAlchemy compares by EQUALITY, so the assignment
    back then emits nothing and a hand-negotiated contract clause is kept
    in memory only. terms_sections is exactly that field (review of
    30335b9)."""
    original = {"terms_sections": [{"heading": "Minimum spend", "body": "$2,000."}]}

    copied = ca.record(original, ["terms_sections"])
    copied["terms_sections"][0]["body"] = "$3,000, negotiated."

    assert original["terms_sections"][0]["body"] == "$2,000.", "the loaded object is untouched"
    assert copied != original, "and the result compares unequal, so an UPDATE is emitted"


def test_a_record_keeps_the_real_names_beside_an_unreadable_one():
    """Discarding a whole record over one bad member silently forgot the
    person's authorship next to it -- failing in the destructive
    direction, which is the one that loses an allergy note."""
    content = {"dietaries": "1x severe nut allergy (table 4).", ca.AUTHORED_KEY: ["dietaries", 42]}
    assert ca.authored(content) == {"dietaries"}
    assert ca.has_record(content) is True


def test_rewriting_a_record_does_not_replace_it_and_cleans_what_it_carried():
    """Reading the old record through authored() is what keeps the
    surviving real names; writing the CLEANED set back is what stops an
    unusable member being carried forward for ever, re-read on every load
    and re-filtered on every write."""
    content = {"dietaries": "allergy", "music": "DJ.", ca.AUTHORED_KEY: ["dietaries", 42, "_reference", ""]}

    updated = ca.record(content, ["music"])

    assert ca.authored(updated) == {"dietaries", "music"}
    assert updated[ca.AUTHORED_KEY] == ["dietaries", "music"], "the junk is gone from the STORED list"


def test_a_record_of_nothing_usable_is_still_no_record():
    """The guarantee the all-or-nothing rule was protecting has to
    survive: a list with no names in it must not read as 'recorded, and
    nobody wrote anything', which sends a reader the opposite way."""
    assert ca.has_record({"dietaries": "a", ca.AUTHORED_KEY: [42, None]}) is False
    assert ca.has_record({"dietaries": "a", ca.AUTHORED_KEY: ["_reference"]}) is False
    # An empty string is not a field name on the READ path either -- the
    # write path refuses it separately, so without this the rule was
    # pinned on only one side (review of b722d81).
    assert ca.authored({"dietaries": "a", ca.AUTHORED_KEY: [""]}) == set()
    assert ca.has_record({"dietaries": "a", ca.AUTHORED_KEY: [""]}) is False
    assert ca.has_record({"dietaries": "a", ca.AUTHORED_KEY: []}) is True, "explicitly empty IS a record"


@pytest.mark.parametrize("bad_keys", [[None], [b"dietaries"], [""], ["dietaries", 42]])
def test_an_unusable_field_name_is_refused_rather_than_recorded_as_nothing(bad_keys):
    """The write path used to filter these, leaving behind a record
    asserting that a person wrote nothing here -- the state the
    bare-string guard exists to prevent -- while the field went
    unrecorded and became destroyable."""
    with pytest.raises(TypeError, match="non-empty"):
        ca.record({"dietaries": "a"}, bad_keys)
    with pytest.raises(TypeError, match="non-empty"):
        ca.forget(ca.record({"dietaries": "a"}, ["dietaries"]), bad_keys)


@pytest.mark.parametrize("blank", [" ", "\t", "\n", "   "])
def test_whitespace_is_not_a_field_name_on_either_path(blank):
    """A form that posts a space, or a caller that strips its input, would
    otherwise write a permanent junk member -- one that reads as real
    authorship for a field no document has, and that no forget() call
    anyone would think to write ever removes."""
    with pytest.raises(TypeError, match="non-blank"):
        ca.record({"dietaries": "a"}, [blank])
    assert ca.authored({"dietaries": "a", ca.AUTHORED_KEY: ["dietaries", blank]}) == {"dietaries"}
    assert ca.has_record({"dietaries": "a", ca.AUTHORED_KEY: [blank]}) is False


def test_a_metadata_key_among_the_names_is_dropped_not_refused():
    """A caller legitimately passes a whole content dict's keys."""
    content = ca.record({"dietaries": "a", "_reference": {}}, ["dietaries", "_reference"])
    assert ca.authored(content) == {"dietaries"}


@pytest.mark.parametrize("not_a_dict", [None, "abc", ["ab", "cd"], 42])
def test_writing_to_non_dict_content_is_refused_rather_than_fabricated(not_a_dict):
    """Reads degrade; writes must not. dict(["ab","cd"]) silently builds
    {'a':'b','c':'d'} -- a JSONB row rewritten into nonsense."""
    with pytest.raises(TypeError, match="must be a dict"):
        ca.record(not_a_dict, ["dietaries"])
    with pytest.raises(TypeError, match="must be a dict"):
        ca.forget(not_a_dict, ["dietaries"])
    with pytest.raises(TypeError, match="must be a dict"):
        ca.carry(not_a_dict, previous={})


# --- carrying a record across a rebuild ---------------------------------------


def test_carry_moves_the_record_onto_freshly_rebuilt_content():
    """A regenerate throws the content away and builds it again, so the
    record goes with it unless somebody moves it. Without this the very
    next regenerate sees no record and treats a person's words as the
    generator's."""
    previous = ca.record({"dietaries": "1x severe nut allergy (table 4).", "timeline": "old"}, ["dietaries"])
    fresh = {"dietaries": "No dietary requirements declared", "timeline": "rebuilt"}

    carried = ca.carry(fresh, previous=previous)

    assert ca.authored(carried) == {"dietaries"}
    assert carried["timeline"] == "rebuilt", "the rebuild's own values are what is kept"
    assert ca.AUTHORED_KEY not in fresh, "the caller's dict is untouched"


def test_carry_does_not_conjure_a_record_the_previous_version_never_had():
    """The obvious spelling -- record(fresh, authored(previous)) -- turns
    'this document predates the record, go and read the audit trail' into
    'authorship was recorded and nobody wrote anything, help yourself'.
    That reversal is what got the first attempt reverted, and carry is
    exactly where it would come back."""
    legacy = {"dietaries": "1x severe nut allergy (table 4)."}
    assert not ca.has_record(legacy)

    carried = ca.carry({"dietaries": "No dietary requirements declared"}, previous=legacy)

    assert ca.has_record(carried) is False, "still unknowable, not 'nobody wrote it'"
    assert ca.authored(carried) == set()


@pytest.mark.parametrize("unreadable", [None, "abc", 42, {"dietaries": "a", ca.AUTHORED_KEY: 42}])
def test_carry_reads_the_previous_version_as_tolerantly_as_anything_else(unreadable):
    """previous is a stored row like any other, so it must not be the
    thing that 500s a regenerate."""
    carried = ca.carry({"dietaries": "regenerated"}, previous=unreadable)
    assert ca.has_record(carried) is False
    assert carried == {"dietaries": "regenerated"}


def test_carry_copies_deeply_too():
    previous = ca.record({"terms_sections": [{"body": "$2,000."}]}, ["terms_sections"])
    fresh = {"terms_sections": [{"body": "$2,000."}]}

    carried = ca.carry(fresh, previous=previous)
    carried["terms_sections"][0]["body"] = "$3,000, hand-negotiated."

    assert fresh["terms_sections"][0]["body"] == "$2,000."
    assert carried != fresh


def test_carry_copies_deeply_on_the_no_record_path_as_well():
    """The early return is a separate write path, and forget's equivalent
    early return was the one that passed the whole suite under two
    separate mutations."""
    fresh = {"terms_sections": [{"body": "$2,000."}]}

    carried = ca.carry(fresh, previous={"dietaries": "legacy"})
    carried["terms_sections"][0]["body"] = "$3,000, hand-negotiated."

    assert carried is not fresh
    assert fresh["terms_sections"][0]["body"] == "$2,000."


# --- where the DEEP part of the copy is actually load-bearing ----------------
#
# record() and forget() normally replace _authored, so the top level differs
# and the write goes out whatever the copy depth. Deepness matters only where
# the top level does NOT change -- and there a shallow copy compares equal to
# the loaded value, so the assignment emits nothing and a caller's nested edit
# is written to memory only. terms_sections is the hand-negotiated contract
# clause (review of b722d81).


def test_recording_a_field_that_is_already_recorded_still_yields_an_unequal_dict():
    """The second edit to an already-recorded field: _authored does not
    change, so only the copy depth separates the result from the loaded
    value."""
    loaded = ca.record({"terms_sections": [{"body": "$2,000."}]}, ["terms_sections"])

    second = ca.record(loaded, ["terms_sections"])
    second["terms_sections"][0]["body"] = "$3,000, hand-negotiated."

    assert loaded["terms_sections"][0]["body"] == "$2,000.", "the loaded object is untouched"
    assert second != loaded, "so the assignment back emits an UPDATE"


def test_forgetting_a_name_that_was_not_recorded_still_yields_an_unequal_dict():
    loaded = ca.record({"terms_sections": [{"body": "$2,000."}]}, ["terms_sections"])

    forgotten = ca.forget(loaded, ["music"])          # not in the record
    forgotten["terms_sections"][0]["body"] = "$3,000, hand-negotiated."

    assert loaded["terms_sections"][0]["body"] == "$2,000."
    assert forgotten != loaded


def test_forgetting_on_content_with_no_record_still_copies_deeply():
    """forget() returns early when there is no record, and that early
    return was the one write path with no copy test behind it -- two
    separate mutations of it passed the whole suite."""
    loaded = {"terms_sections": [{"body": "$2,000."}]}

    forgotten = ca.forget(loaded, ["music"])
    forgotten["terms_sections"][0]["body"] = "$3,000, hand-negotiated."

    assert forgotten is not loaded, "never the caller's own object"
    assert loaded["terms_sections"][0]["body"] == "$2,000."
    assert forgotten != loaded
