"""A template can read a key nobody supplies, and Jinja will not say a word.

The environment uses the default `jinja2.runtime.Undefined`, so `{{ foo }}`
for an unsupplied `foo` renders as an empty string with no error and no log
line. On the invoice's bank block that means three blank fields under a
correct ABN, which reads as a template bug rather than a missing record --
so it gets debugged instead of fixed, while the client is looking at an
invoice they cannot pay.

Until 2026-09-12 that was one typo away: seven identity keys were prefixed
`venue_` and three were not. These tests make the mismatch a failing build
rather than a blank invoice.
"""
import pathlib
import re

import pytest

from app.templating import _IDENTITY_KEYS, templates, venue_identity

# Every template a CLIENT sees. The admin's own pages are excluded on
# purpose: they render through admin_ctx, not venue_identity.
CLIENT_TEMPLATES = ("invoice.html", "document.html")

# `venue_logo_url` is supplied by a Jinja global rather than venue_identity,
# and has_venue_logo() gates it. It is identity-shaped but not part of the
# per-render dict, so it is named here rather than silently tolerated.
SUPPLIED_ELSEWHERE = {"venue_logo_url"}

_IDENTITY_LIKE = re.compile(r"\{\{\s*(venue_[a-z_]+|bank_[a-z_]+)\s*[}|]")


def _identity_placeholders(template_name: str) -> set[str]:
    source = pathlib.Path("app/templates") / template_name
    return set(_IDENTITY_LIKE.findall(source.read_text(encoding="utf-8")))


@pytest.mark.parametrize("template_name", CLIENT_TEMPLATES)
def test_every_identity_key_a_template_reads_is_actually_supplied(template_name):
    """THE guard. A client-facing template may not read an identity key that
    venue_identity does not return -- that renders blank, silently."""
    read = _identity_placeholders(template_name)
    assert read, f"{template_name} reads no identity keys at all -- has the regex drifted?"

    unsupplied = read - set(_IDENTITY_KEYS) - SUPPLIED_ELSEWHERE

    assert not unsupplied, (
        f"{template_name} reads {sorted(unsupplied)}, which venue_identity() does not supply. "
        "Jinja renders these as empty strings with no error, so this ships as a blank field "
        "on a client document."
    )


def test_every_key_is_prefixed_so_the_natural_guess_is_the_right_one():
    """The reason the mismatch existed. A uniform prefix means the name
    somebody guesses is the name that works."""
    unprefixed = [k for k in _IDENTITY_KEYS if not k.startswith("venue_")]

    assert not unprefixed, (
        f"{unprefixed} break the venue_ prefix. Every other key has it, so these are the ones "
        "somebody mistypes -- and a mistyped key renders blank rather than raising."
    )


def test_a_mistyped_key_really_does_render_blank_rather_than_raising():
    """The premise the two tests above rest on, asserted rather than assumed.

    If Jinja were ever configured with StrictUndefined this whole file
    becomes unnecessary -- and this test is what would tell us."""
    rendered = templates.env.from_string("[{{ venue_definitely_not_a_key }}]").render(
        venue_abn="36 654 270 532"
    )

    assert rendered == "[]", (
        "an unknown key no longer renders blank -- if the environment now uses StrictUndefined, "
        "these tests are redundant and can go"
    )


def test_the_dict_and_the_key_list_cannot_drift(hamilton):
    """_IDENTITY_KEYS is what the None-venue branch returns, so a key added
    to one and not the other means a page with no booking behind it renders
    a different set of fields from a page with one."""
    supplied = set(venue_identity(hamilton))
    none_venue = set(venue_identity(None))

    assert supplied == set(_IDENTITY_KEYS), "venue_identity and _IDENTITY_KEYS disagree"
    assert none_venue == set(_IDENTITY_KEYS)


def test_the_bank_block_still_prints_for_a_real_venue(hamilton):
    """The rename must not have quietly emptied the thing it was protecting."""
    identity = venue_identity(hamilton)

    for key in ("venue_bank_account_name", "venue_bank_bsb", "venue_bank_account_number"):
        assert identity[key], f"{key} is empty for the seeded venue"
