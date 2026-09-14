"""No module builds a Hamilton-shaped value at import and leaves it there.

Aaron's rule, 2026-09-03: nothing new hardcodes Hamilton -- the venue comes
from booking.space.venue, and jobs loop venues. The live paths obey it.
What did not were three module constants and a prompt, all computed at
import from venue_profile.default() (which is Hamilton) and all read by
almost nothing:

  * draft_rules.REQUIRED_SIGNATURE_NAME / _VENUE / _EMAIL -- read by
    NOTHING, kept "for anything that imported them". Nothing did.
  * drafting.SYSTEM_PROMPT -- draft_for_booking builds its prompt per call
    from the booking's own venue and never touched this. One test read it,
    which is the only reason it survived.

Neither was a live defect. Both were a trap: the next person needing a
signature name or a system prompt finds one at module scope, uses it, and
puts Hamilton's words on an Entrance draft -- and nothing fails, because
Hamilton is a real venue and the value is a real value.

This test is the trap-setter, not a repeat of the deletion: it fails if
anyone reintroduces the pattern anywhere in app/services.
"""
import ast
import pathlib

import pytest

SERVICES = pathlib.Path("app/services")


def _module_level_calls_to_default(path: pathlib.Path) -> list[str]:
    """Names assigned at MODULE level from a call to venue_profile.default().

    Module level only: inside a function the call is per-request and takes
    whatever venue the caller is working on, which is the correct pattern
    and the one drafting.draft_for_booking uses.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "default":
                out.extend(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
    return out


@pytest.mark.parametrize(
    "path", sorted(SERVICES.glob("*.py")), ids=lambda p: p.name
)
def test_no_service_module_bakes_the_default_venue_at_import(path):
    baked = _module_level_calls_to_default(path)

    assert baked == [], (
        f"{path.name} builds {baked} at import from venue_profile.default(), which is "
        "Hamilton's. A second venue's draft would silently get Hamilton's values. Take "
        "the venue from the booking at the call site instead."
    )


def test_the_deleted_names_stay_deleted():
    """Named explicitly, because a grep for them is the thing a future
    caller would do before writing their own."""
    from app.services import draft_rules, drafting

    for gone in ("REQUIRED_SIGNATURE_NAME", "REQUIRED_SIGNATURE_VENUE", "REQUIRED_SIGNATURE_EMAIL"):
        assert not hasattr(draft_rules, gone), f"{gone} is back -- it is Hamilton's, at import"
    assert not hasattr(drafting, "SYSTEM_PROMPT"), (
        "drafting.SYSTEM_PROMPT is back -- build_system_prompt(profile) per call instead"
    )


def test_the_validator_says_so_when_it_falls_back_to_hamilton(caplog):
    """The fallback stays, because tests call validate() without a profile
    deliberately. What changed is that it is no longer silent: reaching it
    in production means a draft is being checked against the wrong venue's
    house rules and PASSING."""
    import logging

    from app.services import draft_rules, venue_profile

    with caplog.at_level(logging.WARNING, logger="app.services.draft_rules"):
        draft_rules.validate("Hi there, thanks for the enquiry.", client_asked_for_figures=False)

    assert any(
        "no venue profile" in r.getMessage() for r in caplog.records
    ), "the Hamilton fallback is still silent"
    assert any(
        venue_profile.default().trading_name in r.getMessage() for r in caplog.records
    ), "the warning does not say whose rules it fell back to"


def test_passing_a_profile_warns_about_nothing(caplog):
    """The positive control: the live path passes one, and must not be
    telling anybody off for it."""
    import logging

    from app.services import draft_rules, venue_profile

    with caplog.at_level(logging.WARNING, logger="app.services.draft_rules"):
        draft_rules.validate(
            "Hi there, thanks for the enquiry.",
            client_asked_for_figures=False,
            profile=venue_profile.default(),
        )

    assert not any("no venue profile" in r.getMessage() for r in caplog.records)
