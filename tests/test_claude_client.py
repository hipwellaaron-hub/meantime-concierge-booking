"""The one call to the model, and the one way its key can be wrong.

Production drafting failed on every enquiry from 2026-09-04 to 2026-09-06
with "Could not reach the model (LocalProtocolError)": the API key had
been pasted into Railway wrapped across two lines, and the end-stripping
fix of 2026-09-04 left the line break in the middle. h11 refuses a header
value with a line break in it. These tests pin that the key is cleaned
before it is sent, that the cleaning never logs the value, and that if
the request still cannot be formed the recorded reason says what to do.
"""

import logging

import httpx
import pytest

from app.config import settings
from app.services import claude_client


class _Capture:
    def __init__(self, response=None, raises=None):
        self.headers = None
        self.response = response
        self.raises = raises

    def __call__(self, url, *, json, headers, timeout):
        self.headers = headers
        if self.raises:
            raise self.raises
        return self.response


def _ok_response():
    return httpx.Response(200, json={"content": [{"type": "text", "text": "Hi Sam"}]})


@pytest.mark.parametrize("raw", [
    "sk-ant-abc\ndef",
    "sk-ant-abc\r\ndef",
    "sk-ant-abc def",
    "sk-ant-abc​def",
    "﻿sk-ant-abcdef",
    "sk-ant-abc\xa0def",
    "  sk-ant-abcdef\n",
])
def test_the_key_is_sent_as_one_run_of_printable_ascii(monkeypatch, raw, caplog):
    monkeypatch.setattr(settings, "anthropic_api_key", raw)
    capture = _Capture(response=_ok_response())
    monkeypatch.setattr(httpx, "post", capture)
    with caplog.at_level(logging.WARNING, logger="app.services.claude_client"):
        assert claude_client.complete(system="s", user="u") == "Hi Sam"
    assert capture.headers["x-api-key"] == "sk-ant-abcdef"
    # The drop is logged as a count, never the value.
    assert any("cannot be sent in a header" in r.message for r in caplog.records)
    assert not any("sk-ant" in r.getMessage() for r in caplog.records)


def test_a_clean_key_is_sent_untouched_and_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-abcdef")
    capture = _Capture(response=_ok_response())
    monkeypatch.setattr(httpx, "post", capture)
    with caplog.at_level(logging.WARNING, logger="app.services.claude_client"):
        claude_client.complete(system="s", user="u")
    assert capture.headers["x-api-key"] == "sk-ant-abcdef"
    assert caplog.records == []


def test_a_key_with_nothing_sendable_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "\n\n")
    with pytest.raises(claude_client.ClaudeUnavailable, match="Re-paste it in Railway"):
        claude_client.complete(system="s", user="u")


def test_a_malformed_header_says_what_to_do(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-abcdef")
    monkeypatch.setattr(httpx, "post", _Capture(raises=httpx.LocalProtocolError("Illegal header value")))
    with pytest.raises(claude_client.ClaudeUnavailable) as excinfo:
        claude_client.complete(system="s", user="u")
    assert "line break" in str(excinfo.value)
    assert "Re-paste" in str(excinfo.value)


def test_a_wrong_key_reads_as_http_401_not_a_transport_error(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-wrong")
    monkeypatch.setattr(httpx, "post", _Capture(response=httpx.Response(401, json={"error": "x"})))
    with pytest.raises(claude_client.ClaudeUnavailable, match="HTTP 401"):
        claude_client.complete(system="s", user="u")
