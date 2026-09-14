"""A person's sentence beginning [REVIEW] is not a generation placeholder.

THE RULE WAS ALREADY SETTLED, in the comment above
document_regeneration.GENERATED_PLACEHOLDERS, by a review on 2026-09-06:

    "Client bringing cake. [REVIEW] confirm nut-free with kitchen" is a
    human sentence carrying an allergy follow-up, not a placeholder
    (2026-09-06 review -- a substring test silently regenerated over it).

The regenerate path was fixed to exact membership that day. The AI
approval panel in beo_proposals was not: its `replaces_text` still asked
`not existing.lstrip().startswith("[REVIEW]")` three days later. So a
staff member who typed

    [REVIEW] with the kitchen: nut allergy, read it back

into Dietaries had that offered for overwrite under the muted "Replaces a
generation placeholder", instead of the ember-coloured "Replaces text
already on the Event Order" that exists precisely to stop somebody
approving over a colleague's words.

Same defect, second location, and the kind a decorator search cannot see:
it is a string method on a raw value, not a call to the shared rule.
"""
import pytest

from app.services import beo_proposals, document_regeneration


# --- the rule itself ----------------------------------------------------


def test_a_real_generation_placeholder_is_disposable():
    """The positive control. Without it a predicate hardwired to False
    passes every probe below."""
    for placeholder in document_regeneration.GENERATED_PLACEHOLDERS:
        assert document_regeneration.is_disposable(placeholder) is True


def test_an_empty_value_is_disposable():
    assert document_regeneration.is_disposable("") is True


@pytest.mark.parametrize(
    "human",
    [
        "[REVIEW] with the kitchen: nut allergy, read it back",
        "Client bringing cake. [REVIEW] confirm nut-free with kitchen",
        "[REVIEW] add catering order and service style for the SECOND room",
        "[REVIEW] ring Aaron before quoting",
    ],
)
def test_a_human_sentence_that_starts_with_review_is_not_disposable(human):
    """The whole point. Each of these begins with the placeholder prefix
    and none of them is a placeholder -- the last one is the nastiest,
    because it starts with a real placeholder's exact opening words."""
    assert document_regeneration.is_disposable(human) is False


# --- and the panel asks the rule rather than its own version ------------


def test_the_approval_panel_asks_the_shared_rule():
    """Structural, and deliberately so: the two implementations produced
    the same answer for every input EXCEPT the one that mattered, so a
    behavioural probe of the panel proves only that one string. What has
    to hold is that there is ONE rule.
    """
    import inspect

    src = inspect.getsource(beo_proposals.review_rows)

    assert "document_regeneration.is_disposable" in src, (
        "the approval panel no longer asks the shared rule -- a second "
        "opinion about what counts as a placeholder is how this came back"
    )
    # The negative half is NOT a string search for the old expression: the
    # comment recording what was removed contains it verbatim, so that
    # probe failed on the explanation rather than on the code. The AST test
    # below is the one that means it -- it looks at calls, not at prose.


def test_no_module_tests_for_a_placeholder_by_prefix():
    """The trap-setter. A prefix or substring test against the placeholder
    marker anywhere in app/services is the shape of this bug, and it has
    now appeared twice in two different files.

    printed_legacy_music is the one deliberate exception and is named: it
    asks whether a LEGACY MERGED music value is worth promoting, where the
    marker genuinely does start the generated sentence and the value is
    never a free-text note a person typed.

    It is ONE entry because the rule is now in one place. It was two --
    document_regeneration.read_music_as_split and
    beo_proposals._printed_legacy_music, verbatim -- and widening this
    probe to match the constant spelling as well as the literal is what
    found the second copy.
    """
    import ast
    import pathlib

    allowed = {("document_regeneration.py", "printed_legacy_music")}
    offenders = []

    for path in sorted(pathlib.Path("app/services").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("startswith", "find", "index"):
                continue
            # BOTH SPELLINGS. The bug shipped as the string literal; the
            # one deliberate exception spells it as the module constant
            # REVIEW. Matching only literals would have made the allow-list
            # below decorative and the probe weaker than it reads.
            def _names_the_marker(a):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    return "[REVIEW]" in a.value
                return isinstance(a, ast.Name) and a.id == "REVIEW"

            if not any(_names_the_marker(a) for a in node.args):
                continue
            enclosing = "?"
            for outer in ast.walk(tree):
                if isinstance(outer, ast.FunctionDef) and any(
                    n is node for n in ast.walk(outer)
                ):
                    enclosing = outer.name
                    break
            if (path.name, enclosing) in allowed:
                continue
            offenders.append(f"{path.name}:{node.lineno} in {enclosing}()")

    assert not offenders, (
        "these test for a generation placeholder by prefix rather than by exact "
        f"membership in GENERATED_PLACEHOLDERS: {offenders}. A person's sentence "
        "beginning [REVIEW] is not a placeholder -- see the 2026-09-06 note above "
        "GENERATED_PLACEHOLDERS."
    )
