# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""The Ollama provider must never let a prompt be silently truncated.

Ollama drops whatever does not fit its context window without saying so, and the schema map
sits at the front of every prompt — so a window that is too small costs exactly the grounding
the answer depends on. These tests pin the sizing and the warning.
"""

from __future__ import annotations

import pytest

from blossa.config import OllamaConfig
from blossa.llm.http_provider import OllamaProvider, context_window_for


class _Recorder:
    """Stands in for httpx.post and remembers the body it was handed."""

    def __init__(self) -> None:
        self.body: dict = {}

    def __call__(self, url, json, timeout):  # noqa: A002 - httpx's own parameter name
        self.body = json
        return _Response()


class _Response:
    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def json() -> dict:
        return {"message": {"content": "{}"}}


def test_small_prompt_gets_the_floor_not_ollamas_default():
    window, needed = context_window_for(200)
    assert window == 4096
    assert needed < window


def test_window_grows_to_fit_a_real_schema_map():
    # The prompt that started this: a 17-table map, ~48k characters, ~11.7k tokens. Ollama's
    # default 2048-token window kept 2050 of them and threw the rest away.
    window, needed = context_window_for(47_972)
    assert needed > 11_000
    assert window == 16_384


def test_window_never_exceeds_the_cap_and_reports_the_shortfall():
    window, needed = context_window_for(1_000_000, cap=8192)
    assert window == 8192
    assert needed > window  # the caller is expected to warn on this


def test_post_chat_sends_a_window_that_fits(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("blossa.llm.http_provider.httpx.post", recorder)
    provider = OllamaProvider(OllamaConfig())
    provider.generate("system", "u" * 40_000)
    assert recorder.body["options"]["num_ctx"] == 16_384
    assert recorder.body["options"]["temperature"] == 0


def test_an_explicit_num_ctx_is_honoured(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("blossa.llm.http_provider.httpx.post", recorder)
    provider = OllamaProvider(OllamaConfig(num_ctx=2048))
    provider.generate("system", "small")
    assert recorder.body["options"]["num_ctx"] == 2048


def test_a_prompt_that_cannot_fit_warns_instead_of_being_cut_in_silence(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("blossa.llm.http_provider.httpx.post", recorder)
    provider = OllamaProvider(OllamaConfig(max_context_tokens=4096))
    with pytest.warns(UserWarning, match="truncate"):
        provider.generate("system", "u" * 100_000)
    assert recorder.body["options"]["num_ctx"] == 4096
