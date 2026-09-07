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

The first fix (2bbd23e, reverted) bounded every token on the left, which
stopped "minutes" and also stopped "walnuts" and "hazelnuts". Those are
nut allergies. The whole nut family is now an explicit vocabulary that
canonicalises to "nut", and no test in the suite covered the compound
case until this file -- the regression was caught by review, not by a test.
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


@pytest.mark.parametrize("token", beo_rules._DIETARY_TOKENS)
def test_every_token_in_the_list_still_matches_itself(token):
    """A token dropped from the pattern table by accident would silently
    stop being protected. The nut family answers as "nut"."""
    found = declared_dietaries(f"1x {token} requirement")
    expected = "nut" if token in ("nut", "peanut") else token
    assert expected in found, (token, found)


def test_the_existing_incident_case_is_unchanged():
    """The one pre-existing test for this rule, restated here so the two
    cannot drift apart."""
    result = beo_rules.validate(
        {"dietaries": "2x vegetarian"}, current={"dietaries": "2x vegetarian, 1x severe nut allergy"}
    )
    assert beo_rules.DROPS_DIETARY in result.codes
    assert "nut" in result.as_note()
