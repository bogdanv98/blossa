# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Configuration model and loader.

Precedence (highest wins): CLI flags > environment variables (BLOSSA_*) > config file > defaults.
Nested env vars use a double underscore, e.g. BLOSSA_ORACLE__PASSWORD, BLOSSA_LLM__OLLAMA__MODEL.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OracleConfig(BaseModel):
    dsn: str = "localhost:1521/XEPDB1"
    user: str = "blossa_demo"
    password: str = "blossa_demo"
    schema_name: str | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:14b"
    timeout: int = 120


class OpenAICompatibleConfig(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key: str = ""
    timeout: int = 120


class LLMConfig(BaseModel):
    provider: str = "ollama"  # ollama | heuristic | openai_compatible
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openai_compatible: OpenAICompatibleConfig = Field(default_factory=OpenAICompatibleConfig)


class ScanConfig(BaseModel):
    max_tables: int = 0
    sample_rows: int = 1000
    candidate_fk_overlap: float = 0.85
    skip_profiling: bool = False


class OutputConfig(BaseModel):
    dir: str = "out"
    name: str = "database_map"


class Settings(BaseSettings):
    oracle: OracleConfig = Field(default_factory=OracleConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    model_config = SettingsConfigDict(
        env_prefix="BLOSSA_",
        env_nested_delimiter="__",
        extra="ignore",
    )


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from a YAML file (if given/found) and overlay environment variables."""
    file_data: dict = {}
    path = _resolve_config_path(config_path)
    if path is not None:
        with path.open("r", encoding="utf-8") as fh:
            file_data = yaml.safe_load(fh) or {}

    # BaseSettings merges env vars on top of the values we pass in from the file.
    return Settings(**file_data)


def _resolve_config_path(config_path: str | Path | None) -> Path | None:
    if config_path:
        p = Path(config_path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        return p
    # Auto-discover, preferring the local (git-ignored) override.
    for candidate in ("blossa.local.yml", "blossa.yml"):
        p = Path(candidate)
        if p.exists():
            return p
    return None
