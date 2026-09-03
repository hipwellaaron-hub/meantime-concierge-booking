"""House rules as blocking validators (brief section 6).

Every rule is tested in both directions. A validator that blocks a
perfectly good draft is its own failure mode, and a false block is
invisible to the person it inconveniences, so the "clean draft passes"
cases matter as much as the violations.
"""

import pytest

from app.services import draft_rules

CLEAN = """Hi Sarah,

Thanks for getting in touch about your 30th on Saturday 14 November.

The Loft is available that night and would suit 80 guests nicely. Our
kitchen is 100% gluten free, which catches most people by surprise, and
the food comes out as platters to share.

Drinks work as a bar tab on the night, most groups budget around $25 per
person as a guide.

If you would like to see the space, we do walkthroughs Wednesday through
Sunday between 3 and 5pm (we are closed Monday and Tuesday).

Would you like me to hold the date while you think it over?

Aaron
Meantime Hamilton
meantimehamilton@gmail.com
"""


def _clean_with(extra: str) -> str:
    return CLEAN.replace("Would you like me to hold", f"{extra}\n\nWould you like me to hold")


# --- the clean draft passes ---------------------------------------------


def test_a_clean_draft_passes(db=None):
    result = draft_rules.validate(CLEAN)
    assert result.blocked is False, [v.code for v in result.blocks]


def test_a_clean_draft_raises_no_warning_either():
    assert draft_rules.validate(CLEAN).warnings == []


# --- each blocking rule -------------------------------------------------


def test_an_em_dash_blocks():
    result = draft_rules.validate(CLEAN.replace("nicely.", "nicely — really nicely."))
    assert result.blocked
    assert draft_rules.EM_DASH in result.codes


def test_a_missing_signature_blocks():
    result = draft_rules.validate(CLEAN.replace("meantimehamilton@gmail.com", ""))
    assert result.blocked
    assert draft_rules.SIGNATURE in result.codes


def test_a_balance_owing_blocks_unless_the_client_asked():
    text = _clean_with("Your balance owing is $2,400.")
    assert draft_rules.validate(text).blocked
    assert draft_rules.AMOUNT_OWING in draft_rules.validate(text).codes

    # The one relaxation, and it comes from the caller, never the draft.
    allowed = draft_rules.validate(text, client_asked_for_figures=True)
    assert draft_rules.AMOUNT_OWING not in allowed.codes


def test_a_per_head_guide_is_not_mistaken_for_a_balance():
    """$25 per person as a guide is explicitly correct and must pass."""
    assert draft_rules.AMOUNT_OWING not in draft_rules.validate(CLEAN).codes


def test_a_beverage_package_blocks():
    for phrasing in ["our beverage packages start at", "we offer a drinks package", "a bar package"]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.BEVERAGE_PACKAGE in result.codes, phrasing


def test_stating_how_many_a_platter_feeds_blocks():
    for phrasing in [
        "Each platter feeds 10 people.",
        "The antipasto platter serves about 8 guests.",
        "We suggest one platter, it caters for 12.",
    ]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.PLATTER_SERVES in result.codes, phrasing


def test_describing_platters_without_a_headcount_passes():
    ok = _clean_with("Each platter is roughly 25 pieces, about four entree sized serves.")
    assert draft_rules.PLATTER_SERVES not in draft_rules.validate(ok).codes


def test_disclosing_the_rsa_procedure_blocks():
    for phrasing in [
        "Guests under 18 are given a wristband on arrival.",
        "We will check their ID at the door.",
        "Our staff stamp the hands of minors.",
    ]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.RSA_PROCEDURE in result.codes, phrasing


def test_promising_setup_access_blocks():
    for phrasing in [
        "You can set up from 2pm.",
        "Your bump-in is confirmed for 3pm.",
        "Early access is guaranteed on the day.",
    ]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.PROMISED_ACCESS in result.codes, phrasing


def test_offering_setup_access_as_a_request_passes():
    ok = _clean_with("I can ask about setup access from 2pm, though I cannot promise it until closer to the day.")
    assert draft_rules.PROMISED_ACCESS not in draft_rules.validate(ok).codes


def test_a_walkthrough_without_the_days_blocks():
    """A client once arrived on a closed Monday because this was omitted."""
    text = CLEAN.replace(
        "we do walkthroughs Wednesday through\nSunday between 3 and 5pm (we are closed Monday and Tuesday)",
        "you are welcome to come in for a walkthrough any time",
    )
    result = draft_rules.validate(text)
    assert result.blocked
    assert draft_rules.WALKTHROUGH_HOURS in result.codes


def test_a_draft_with_no_walkthrough_offer_is_not_asked_for_hours():
    text = CLEAN.replace(
        "If you would like to see the space, we do walkthroughs Wednesday through\n"
        "Sunday between 3 and 5pm (we are closed Monday and Tuesday).",
        "",
    )
    assert draft_rules.WALKTHROUGH_HOURS not in draft_rules.validate(text).codes


# --- the one warning ----------------------------------------------------


def test_omitting_the_gluten_free_kitchen_warns_but_does_not_block():
    text = CLEAN.replace("Our\nkitchen is 100% gluten free, which catches most people by surprise, and\nthe", "The")
    result = draft_rules.validate(text)
    assert draft_rules.GLUTEN_FREE in result.codes
    assert result.blocked is False, "a missing selling point must not withhold a safe draft"


# --- fail closed --------------------------------------------------------


def test_validation_fails_closed(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(draft_rules, "_validate", explode)
    result = draft_rules.validate(CLEAN)
    assert result.blocked
    assert draft_rules.RULES_ERROR in result.codes


def test_several_violations_are_all_reported():
    text = _clean_with("Each platter feeds 10 people — and your balance owing is $2,400.")
    codes = draft_rules.validate(text).codes
    assert {draft_rules.EM_DASH, draft_rules.PLATTER_SERVES, draft_rules.AMOUNT_OWING} <= set(codes)


# --- the allergen safety claim (blocking) -------------------------------


def test_claiming_the_kitchen_is_nut_free_blocks():
    """The kitchen is 100% gluten free and is NOT nut free. A wrong claim
    here is a safety issue, not an embarrassment."""
    for phrasing in [
        "Our kitchen is nut free so that will be fine.",
        "The platters are allergen free.",
        "That platter is safe for a nut allergy.",
        "Everything is free of nuts.",
        "There is no risk of cross contamination.",
        "I can guarantee it is nut free.",
        "The menu is dairy free throughout.",
    ]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.ALLERGEN_CLAIM in result.codes, phrasing
        assert result.blocked, phrasing


def test_honestly_denying_nut_free_status_passes():
    """Saying we are NOT nut free is exactly right and must not block."""
    for phrasing in [
        "Our kitchen is 100% gluten free, but we are not a nut free kitchen.",
        "We cannot guarantee nut free, so please let me know about allergies.",
        "The kitchen is not nut free.",
    ]:
        result = draft_rules.validate(_clean_with(phrasing))
        assert draft_rules.ALLERGEN_CLAIM not in result.codes, phrasing


def test_the_gluten_free_claim_itself_still_passes():
    """100% gluten free is true and is the differentiator worth stating."""
    assert draft_rules.ALLERGEN_CLAIM not in draft_rules.validate(CLEAN).codes
