"""Dietary tokens match words, never bare substrings.

declared_dietaries() is half of DROPS_DIETARY: the set of requirements the
current Event Order declares, minus the set the proposal declares, must be
empty or the proposal is refused. The match was a bare substring test, so
"nut" fired inside "minutes" and "egg" inside "eggplant". A current value
of "Kitchen needs 20 minutes notice." therefore declared a nut allergy,
and any proposal that did not repeat the word was REFUSED for dropping it.

Refused, not warned. These rules block by Aaron's ruling, because a
warning nobody sees is worse than a block -- and a blocked proposal is
stored and never shown to staff. So the false match made a legitimate
transcription silently vanish. Worse than when it was first found.

Two attempts have failed here, in opposite directions, and this file exists
to hold both open at once:

  - the substring test over-matched (minutes, eggplant), refusing real work;
  - a plain word boundary on both sides under-matched, dropping "walnuts",
    "2xvegan", "glutenfree" and "nut-free" -- spellings staff actually type,
    where a miss is a declared requirement leaving the Event Order in
    silence.

So the boundary names what may sit beside a token (a count on the left, a
plural or a free/less compound on the right), any word ending in "nut" is a
nut by RULE with a short list of foods excluded rather than a closed list of
nuts included, peanut answers as its own allergen as well as a nut, and an
invisible character pasted from email cannot hide a declaration in either
direction.
"""

import pytest

from app.services import beo_rules
from app.services.beo_rules import declared_dietaries

# --- the bug --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Kitchen needs 20 minutes notice.",
        "Cake cut at 9pm, allow 15 minutes.",
        "1x eggplant parmigiana for the table",
        "Roast butternut pumpkin for the vegetarians",  # vegetarian yes, nut no
        "Doughnuts on the dessert table",
    ],
)
def test_a_word_that_merely_contains_a_token_declares_nothing_of_that_token(text):
    found = declared_dietaries(text)
    assert "nut" not in found, (text, found)
    assert "egg" not in found, (text, found)


def test_twenty_minutes_notice_no_longer_blocks_a_transcription():
    """The live defect, end to end through the rule. Before this the
    proposal was refused for dropping a nut allergy that was never
    declared -- and refused means stored and never shown."""
    result = beo_rules.validate(
        {"dietaries": "2x vegetarian."},
        current={"dietaries": "2x vegetarian. Kitchen needs 20 minutes notice."},
    )
    assert beo_rules.DROPS_DIETARY not in result.codes
    assert not result.blocked


# --- the regression the first fix caused ----------------------------------------


@pytest.mark.parametrize(
    "text",
    ["walnuts", "1x walnut allergy", "hazelnut", "Hazelnuts in the dessert -- 1x allergy",
     "peanut", "peanuts", "cashews", "almond", "pistachio", "macadamia", "pecans", "chestnut",
     "coconut", "nut-free please", "pine nut", "1x severe nut allergy"],
)
def test_every_nut_is_a_nut(text):
    assert "nut" in declared_dietaries(text), text


def test_a_walnut_allergy_and_a_nut_allergy_are_the_same_declaration():
    """Canonical tokens are what make the set difference honest: the current
    value says walnut, the proposal says nut, nothing was dropped."""
    result = beo_rules.validate(
        {"dietaries": "1x nut allergy (table 4)."},
        current={"dietaries": "1x walnut allergy (table 4)."},
    )
    assert beo_rules.DROPS_DIETARY not in result.codes


def test_dropping_a_walnut_allergy_is_still_caught_and_named_as_nut():
    result = beo_rules.validate(
        {"dietaries": "2x vegetarian"},
        current={"dietaries": "2x vegetarian, 1x walnut allergy"},
    )
    assert beo_rules.DROPS_DIETARY in result.codes
    assert "nut" in result.as_note()


# --- plurals, stems and hyphens still match ---------------------------------------


@pytest.mark.parametrize(
    "text, token",
    [
        ("vegetarians", "vegetarian"),
        ("2x vegans", "vegan"),
        ("eggs", "egg"),
        ("egg-free", "egg"),
        ("soya", "soy"),
        ("soy sauce", "soy"),
        ("dairy-free", "dairy"),
        ("gluten-free", "gluten"),
        ("allergies", "allerg"),
        ("allergic to shellfish", "allerg"),
        ("anaphylactic", "anaphyla"),
        ("lactose intolerance", "intoleran"),
        ("low-FODMAP", "fodmap"),
        ("carries an EpiPen", "epipen"),
    ],
)
def test_ordinary_spellings_still_declare(text, token):
    assert token in declared_dietaries(text), text


# A FROZEN list, deliberately not derived from _DIETARY_TOKENS. The previous
# version parametrized over the same tuple that builds the pattern table, so
# deleting a token deleted its own test case: removing "sesame" left 208
# tests passing while DROPS_DIETARY quietly stopped protecting a declared
# sesame allergy (review of 44c84b5). A guard that is generated from the
# thing it guards cannot notice the thing going missing.
PROTECTED_VOCABULARY = (
    "nut", "peanut", "gluten", "coeliac", "celiac", "dairy", "lactose", "vegan",
    "vegetarian", "halal", "kosher", "shellfish", "seafood", "sesame", "egg", "soy",
    "pescatarian", "allerg", "anaphyla", "intoleran", "epipen", "fodmap",
)


@pytest.mark.parametrize("token", PROTECTED_VOCABULARY)
def test_every_protected_requirement_still_declares(token):
    """Deleting a token from the vocabulary must fail HERE, loudly, rather
    than quietly stop protecting a requirement."""
    found = declared_dietaries(f"1x {token} requirement")
    assert token in found, (token, found)


def test_the_vocabulary_has_not_silently_shrunk():
    """The other half: every plain token the module ships is on the frozen
    list above, so ADDING one without covering it here fails too."""
    shipped = set(beo_rules._DIETARY_TOKENS) | {"nut", "peanut"}
    assert shipped == set(PROTECTED_VOCABULARY), (
        f"vocabulary changed: {shipped ^ set(PROTECTED_VOCABULARY)}"
    )


def test_the_existing_incident_case_is_unchanged():
    """The one pre-existing test for this rule, restated here so the two
    cannot drift apart."""
    result = beo_rules.validate(
        {"dietaries": "2x vegetarian"}, current={"dietaries": "2x vegetarian, 1x severe nut allergy"}
    )
    assert beo_rules.DROPS_DIETARY in result.codes
    assert "nut" in result.as_note()


# --- run-together spellings staff actually type (review of 44c84b5) ----------


@pytest.mark.parametrize(
    "text, token",
    [
        ("2xvegan", "vegan"), ("12xvegetarian", "vegetarian"), ("1xnut allergy", "nut"),
        ("glutenfree", "gluten"), ("nutfree", "nut"), ("dairyfree", "dairy"),
        ("soyfree", "soy"), ("lactosefree", "lactose"), ("eggless", "egg"),
        ("gluten free", "gluten"), ("nut-free", "nut"),
    ],
)
def test_a_spelling_with_no_space_still_declares(text, token):
    """A plain word boundary on both sides dropped every one of these, so a
    requirement typed as shorthand could be dropped by a transcription with
    nothing said -- and, in the other direction, re-typing "2x vegan" as
    "2xvegan" was refused as dropping vegan."""
    assert token in declared_dietaries(text), text


def test_shorthand_is_the_same_declaration_as_the_spaced_form():
    assert not beo_rules.validate(
        {"dietaries": "2xvegan"}, current={"dietaries": "2 x vegan"}
    ).blocked
    assert beo_rules.DROPS_DIETARY in beo_rules.validate(
        {"dietaries": "no requirements"}, current={"dietaries": "2xvegan, 1x nut allergy"}
    ).codes


# --- any word ending in nut is a nut, by rule not by list --------------------


@pytest.mark.parametrize(
    "text",
    ["pinenut", "pine nuts", "groundnut", "groundnuts", "brazilnut", "treenut",
     "chestnut", "coconut", "walnut", "hazelnuts"],
)
def test_a_compound_nobody_listed_is_still_a_nut(text):
    """The previous fix used a closed list and missed exactly the compounds
    nobody thought of, which on this field is a silent loss of safety data."""
    assert "nut" in declared_dietaries(text), text


@pytest.mark.parametrize("text", ["Roast butternut pumpkin", "Doughnuts", "donuts", "a donut wall"])
def test_a_food_that_merely_ends_in_nut_is_not_a_nut(text):
    assert "nut" not in declared_dietaries(text), text


@pytest.mark.parametrize("text", ["soybean", "soybeans", "soymilk", "soya", "soy sauce"])
def test_soy_compounds_declare_soy(text):
    assert "soy" in declared_dietaries(text), text


# --- peanut is its own allergen ----------------------------------------------


def test_a_peanut_allergy_declares_both_peanut_and_nut():
    assert declared_dietaries("1x peanut allergy") >= {"nut", "peanut"}
    assert "peanut" not in declared_dietaries("1x cashew allergy")


def test_swapping_a_peanut_allergy_for_a_tree_nut_is_a_dropped_requirement():
    """Peanut is a legume and a distinct allergen -- the venue's own
    catalogue keeps it separate on MenuItem.contains_peanuts. Canonicalising
    every nut to one token made this substitution invisible."""
    result = beo_rules.validate(
        {"dietaries": "1x cashew allergy (anaphylaxis, EpiPen)."},
        current={"dietaries": "1x peanut allergy (anaphylaxis, EpiPen)."},
    )
    assert beo_rules.DROPS_DIETARY in result.codes

    # and the reverse is fine: a tree-nut allergy restated as peanut ADDS
    assert not beo_rules.validate(
        {"dietaries": "1x peanut allergy."}, current={"dietaries": "1x nut allergy."}
    ).blocked


# --- an invisible character cannot hide a declaration ------------------------


@pytest.mark.parametrize(
    "text",
    ["nut\u200ballergy", "gluten\u2060free", "n\u200but allergy", "vegan\u00adx2"],
)
def test_a_pasted_invisible_character_cannot_hide_a_requirement(text):
    """normalise() DELETES invisibles, which rejoins a word split in half
    ("n<ZWSP>ut") but glues two words together ("nut<ZWSP>allergy") -- and a
    word-boundary match then sees neither. Both spellings are read and the
    union taken, so a paste from email or Word cannot drop a declaration."""
    assert declared_dietaries(text), text


def test_an_invisible_between_words_still_blocks_a_drop():
    result = beo_rules.validate(
        {"dietaries": "No dietary requirements"},
        current={"dietaries": "1x nut\u200ballergy (table 4)."},
    )
    assert beo_rules.DROPS_DIETARY in result.codes
