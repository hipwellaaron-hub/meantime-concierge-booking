"""Whatever a client types under the cake cards reaches the Event Order.

One textarea sits under the cake options and the wizard saves it as
`cake_notes` whatever is selected. The Event Order's special-notes block
read it on ONE branch: `cake_choice.type == "outside"`. So two things a
client typed went nowhere at all.

  * "please write Happy 40th Sarah in white" alongside an IN-HOUSE cake.
    That is the common case, and the one the kitchen needs.

  * anything typed when the catalogue has no cake cards to click. The step
    itself says, in that state, "our current cake options are being
    finalized — describe what you'd like below and we'll confirm with
    you". With outside cake not permitted (the default) there is nothing
    to select, so `type` stays "none" — and the description the client was
    expressly asked for reached nobody. A promise the page makes and the
    generator does not keep.

The review screen shows only the TYPE, so nothing downstream said the note
had been dropped either.
"""
import pytest

from app.services.wizard_generation import build_special_notes


def _notes(**cake):
    """The lines the Event Order's special-notes block prints, for a
    booking whose wizard answer carries this cake choice and nothing else.

    build_special_notes returns them newline-joined (or None when there is
    nothing to say), so this splits them back out -- asserting on a list
    keeps "says nothing about cake" a real assertion rather than a
    substring that happens not to appear.
    """
    block = build_special_notes(extras_response={"cake_choice": cake}, booking=None, vendors=[])
    return (block or "").splitlines()


def test_an_in_house_cake_carries_its_note():
    """THE common one. A note beside an in-house selection used to vanish:
    the branch appended "see Desserts in the Food Order" and dropped the
    words."""
    lines = _notes(type="in_house", menu_item_id=None, notes="Happy 40th Sarah, in white")

    blob = " ".join(lines)
    assert "Happy 40th Sarah, in white" in blob
    assert "see Desserts in the Food Order" in blob, (
        "the pointer to the priced line was lost while adding the note"
    )


def test_a_described_cake_with_nothing_selected_still_arrives():
    """The state the wizard's own fallback copy describes. Nothing is
    selectable, the client is asked to describe what they want, and type
    stays none."""
    lines = _notes(type="none", menu_item_id=None, notes="Three tiers, lemon, no fondant")

    blob = " ".join(lines)
    assert "Three tiers, lemon, no fondant" in blob
    assert "confirm" in blob.lower(), (
        "a described-but-unselected cake arrives with no sign that nobody has "
        "priced or agreed it"
    )


def test_an_outside_cake_still_carries_its_note_and_its_rule():
    """The branch that already worked. Kept as a control: a fix that moved
    the note out of this branch would pass the two probes above and lose
    the gluten-free sentence that goes with it."""
    lines = _notes(type="outside", menu_item_id=None, notes="Collecting it at 5pm")

    blob = " ".join(lines)
    assert "Collecting it at 5pm" in blob
    assert "gluten-free only" in blob
    assert "not in the kitchen" in blob


def test_no_cake_and_no_note_says_nothing_about_cake():
    """The positive control for silence. Without it, an implementation that
    printed a Cake line unconditionally would pass everything above and put
    "Cake:" on every Event Order."""
    lines = _notes(type="none", menu_item_id=None, notes=None)

    assert not any(line.startswith("Cake") for line in lines)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_whitespace_is_not_a_note(blank):
    """A textarea a client touched and cleared is not a description."""
    lines = _notes(type="none", menu_item_id=None, notes=blank)

    assert not any(line.startswith("Cake") for line in lines)


def test_an_in_house_cake_without_a_note_reads_as_it_always_did():
    lines = _notes(type="in_house", menu_item_id=None, notes=None)

    blob = " ".join(lines)
    assert "see Desserts in the Food Order" in blob
    # No trailing space, no empty tail -- the note is appended with strip().
    assert not any(line.endswith(" ") for line in lines)
