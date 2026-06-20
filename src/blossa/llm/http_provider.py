# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP-backed LLM providers: a local Ollama server, or any OpenAI-compatible endpoint.

Both speak to a local/self-hosted model by default; the only external traffic is to the
base_url you configure. With Ollama on localhost, Blossa makes no off-box network calls.
"""

from __future__ import annotations

import httpx

from ..config import OllamaConfig, OpenAICompatibleConfig
from ..models import TableSemantics, TableSummary
from .base import SYSTEM_PROMPT, LLMProvider, build_user_prompt, parse_response


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, config: OllamaConfig):
        self._config = config
        self.model = config.model

    def available(self) -> bool:
        try:
            resp = httpx.get(f"{self._config.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def analyze(self, summary: TableSummary) -> TableSemantics:
        body = {
            "model": self._config.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(summary)},
            ],
        }
        resp = httpx.post(
            f"{self._config.base_url}/api/chat", json=body, timeout=self._config.timeout
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return parse_response(summary, content)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, config: OpenAICompatibleConfig):
        self._config = config
        self.model = config.model

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

    def analyze(self, summary: TableSummary) -> TableSemantics:
        body = {
            "model": self._config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(summary)},
            ],
        }
        resp = httpx.post(
            f"{self._config.base_url}/chat/completions",
            json=body,
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return parse_response(summary, content)
