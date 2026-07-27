# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP-backed LLM providers: a local Ollama server, or any OpenAI-compatible endpoint.

Both speak to a local/self-hosted model by default; the only external traffic is to the
base_url you configure. With Ollama on localhost, Blossa makes no off-box network calls.
"""

from __future__ import annotations

import warnings

import httpx

from ..config import OllamaConfig, OpenAICompatibleConfig
from ..models import TableSemantics, TableSummary
from .base import SYSTEM_PROMPT, LLMProvider, build_user_prompt, parse_response

# Ollama discards whatever does not fit the context window and reports nothing — no error, no
# flag on the response. The system rules and the schema map sit at the FRONT of the prompt, so a
# window that is too small removes exactly the grounding the answer depends on, and the model
# then answers about a plausible invented schema instead of the real one. The default window is
# 2048 tokens; a map of a 17-table schema needs roughly six times that. So ask for a window that
# fits the prompt we are actually sending.
#
# Measured on qwen2.5:14b, prompts of this shape (JSON, Oracle identifiers, English + Romanian
# prose) run about 4.1 characters per token. Dividing by 3.5 overestimates the token count on
# purpose: guessing high costs a little KV cache, guessing low costs the whole schema map.
_CHARS_PER_TOKEN = 3.5
_RESPONSE_HEADROOM = 1024  # the reply shares the window with the prompt
_MIN_CONTEXT = 4096  # below this, small prompts pay for a window sizing they never use


def context_window_for(prompt_chars: int, cap: int = 32768) -> tuple[int, int]:
    """Pick a context window for a prompt of this size.

    Returns (window, needed): the window to request, and the tokens the prompt is estimated to
    need. When needed > window the prompt will be truncated — the caller warns rather than
    letting it happen quietly.
    """
    needed = int(prompt_chars / _CHARS_PER_TOKEN) + _RESPONSE_HEADROOM
    window = _MIN_CONTEXT
    while window < needed:
        window *= 2
    return min(window, cap), needed


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, config: OllamaConfig, language: str = "en"):
        self._config = config
        self.model = config.model
        self.language = language

    def available(self) -> bool:
        try:
            resp = httpx.get(f"{self._config.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def _num_ctx(self, prompt_chars: int) -> int:
        cap = self._config.max_context_tokens
        window, needed = context_window_for(prompt_chars, cap)
        if self._config.num_ctx:
            window = self._config.num_ctx
        if needed > window:
            warnings.warn(
                f"This prompt needs about {needed} tokens but the context window is {window}. "
                f"Ollama will truncate it from the front, dropping part of the schema map, and "
                f"the answer may reference tables that do not exist. Raise "
                f"llm.ollama.max_context_tokens (currently {cap}) or scan fewer schemas at once.",
                stacklevel=3,
            )
        return window

    def _post_chat(self, system_prompt: str, user_prompt: str) -> str:
        body = {
            "model": self._config.model,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": self._num_ctx(len(system_prompt) + len(user_prompt)),
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        resp = httpx.post(
            f"{self._config.base_url}/api/chat", json=body, timeout=self._config.timeout
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    def analyze(self, summary: TableSummary) -> TableSemantics:
        content = self._post_chat(SYSTEM_PROMPT, build_user_prompt(summary, self.language))
        return parse_response(summary, content)

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self._post_chat(system_prompt, user_prompt)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, config: OpenAICompatibleConfig, language: str = "en"):
        self._config = config
        self.model = config.model
        self.language = language

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def available(self) -> bool:
        try:
            resp = httpx.get(
                f"{self._config.base_url}/models", headers=self._headers(), timeout=5
            )
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    def _post_chat(self, system_prompt: str, user_prompt: str) -> str:
        body = {
            "model": self._config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        resp = httpx.post(
            f"{self._config.base_url}/chat/completions",
            json=body,
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def analyze(self, summary: TableSummary) -> TableSemantics:
        content = self._post_chat(SYSTEM_PROMPT, build_user_prompt(summary, self.language))
        return parse_response(summary, content)

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self._post_chat(system_prompt, user_prompt)
