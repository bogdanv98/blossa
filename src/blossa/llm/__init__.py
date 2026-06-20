# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Pluggable LLM providers for the semantic pass.

Default is a local model via Ollama (privacy / EU-residency story). A fully offline
rule-based `heuristic` provider is always available as a fallback and for CI.
"""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMProvider
from .heuristic import HeuristicProvider
from .http_provider import OllamaProvider, OpenAICompatibleProvider


def get_provider(config: LLMConfig) -> LLMProvider:
    provider = config.provider.lower()
    if provider == "ollama":
        return OllamaProvider(config.ollama)
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(config.openai_compatible)
    if provider == "heuristic":
        return HeuristicProvider()
    raise ValueError(
        f"Unknown LLM provider '{config.provider}'. "
        "Expected one of: ollama, openai_compatible, heuristic."
    )


__all__ = [
    "LLMProvider",
    "HeuristicProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "get_provider",
]
