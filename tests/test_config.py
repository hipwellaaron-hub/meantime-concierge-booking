"""Settings loads Railway/`.env` values as-is except for one thing: every
string field is stripped of leading/trailing whitespace. See app.config's
own comment on why -- a real incident, 2026-09-04.
"""

from app.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(database_url="postgresql://x", secret_key="y")
    defaults.update(overrides)
    return Settings(**defaults)


def test_a_trailing_newline_on_the_anthropic_key_is_stripped():
    # The exact failure: a copy-paste artifact left a trailing newline on
    # ANTHROPIC_API_KEY. httpx refuses outright to send a header value
    # containing a raw newline (httpx.LocalProtocolError) -- every
    # drafting attempt recorded "Could not reach the model
    # (LocalProtocolError)" even though the key itself was correct.
    s = _settings(anthropic_api_key="sk-ant-abc123\n")
    assert s.anthropic_api_key == "sk-ant-abc123"


def test_leading_and_trailing_whitespace_is_stripped_from_any_string_field():
    s = _settings(database_url="  postgresql://x  ", secret_key=" y\t")
    assert s.database_url == "postgresql://x"
    assert s.secret_key == "y"


def test_internal_whitespace_is_left_alone():
    # str_strip_whitespace only trims the ends -- unlike the Gmail
    # app-password fix (which removes whitespace everywhere, since that
    # value is legitimately displayed with internal spaces), nothing here
    # has meaningful internal whitespace to preserve, but this pins that
    # the behaviour is ends-only, not a blunt "remove all whitespace".
    s = _settings(ai_draft_model="claude sonnet 5")
    assert s.ai_draft_model == "claude sonnet 5"
