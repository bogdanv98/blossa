# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Prerequisite checks shared by `blossa doctor` and the `blossa scan` pre-flight.

Each check returns a CheckResult with an honest status and, when something is wrong, a
copy-pasteable hint on how to fix it. The goal is that a brand-new user never has to guess
what is missing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import httpx

from .config import Settings, _resolve_config_path

MIN_PYTHON = (3, 12)


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    hint: str = ""


@dataclass
class Diagnosis:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(r.status == Status.FAIL for r in self.results)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)


# --------------------------------------------------------------------- checks


def check_python() -> CheckResult:
    v = sys.version_info
    current = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        return CheckResult("Python", Status.OK, f"{current}")
    return CheckResult(
        "Python",
        Status.FAIL,
        f"{current} (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        hint="Install Python 3.12+ from https://www.python.org/downloads/",
    )


def check_oracledb() -> CheckResult:
    try:
        import oracledb  # noqa: PLC0415 - imported lazily so doctor works even if missing
    except ImportError:
        return CheckResult(
            "oracledb driver",
            Status.FAIL,
            "not installed",
            hint="pip install oracledb",
        )
    return CheckResult(
        "oracledb driver", Status.OK, f"{oracledb.__version__} (thin mode, no client needed)"
    )


def check_config(config_path: str | Path | None) -> CheckResult:
    try:
        resolved = _resolve_config_path(config_path)
    except FileNotFoundError as exc:
        return CheckResult("Config file", Status.FAIL, str(exc),
                           hint="Run `blossa init` to create blossa.local.yml")
    if resolved is None:
        return CheckResult(
            "Config file",
            Status.WARN,
            "none found (using defaults / env vars)",
            hint="Run `blossa init` to create blossa.local.yml",
        )
    return CheckResult("Config file", Status.OK, str(resolved))


def check_oracle_connection(settings: Settings) -> CheckResult:
    try:
        import oracledb  # noqa: PLC0415
    except ImportError:
        return CheckResult("Oracle connection", Status.FAIL, "oracledb not installed",
                           hint="pip install oracledb")
    try:
        conn = oracledb.connect(
            user=settings.oracle.user,
            password=settings.oracle.password,
            dsn=settings.oracle.dsn,
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        return CheckResult(
            "Oracle connection",
            Status.FAIL,
            f"cannot connect to {settings.oracle.dsn} as {settings.oracle.user}",
            hint=(
                f"{_short(exc)}\n"
                "      Check dsn/user/password (set the password via "
                "BLOSSA_ORACLE__PASSWORD), or start the demo DB: cd docker && docker compose up -d"
            ),
        )
    try:
        owner = (settings.oracle.schema_name or settings.oracle.user).upper()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = :o", {"o": owner}
            )
            (table_count,) = cur.fetchone()
    finally:
        conn.close()
    if table_count == 0:
        return CheckResult(
            "Oracle connection",
            Status.WARN,
            f"connected, but schema {owner} has 0 visible tables",
            hint="Set the right `schema` in your config, or grant SELECT on the target schema.",
        )
    return CheckResult(
        "Oracle connection", Status.OK, f"connected; {owner} has {table_count} table(s)"
    )


def check_llm(settings: Settings) -> CheckResult:
    provider = settings.llm.provider.lower()
    if provider == "heuristic":
        return CheckResult("LLM provider", Status.OK, "heuristic (offline, no model needed)")
    if provider == "ollama":
        return _check_ollama(settings)
    if provider == "openai_compatible":
        return _check_openai(settings)
    return CheckResult("LLM provider", Status.FAIL, f"unknown provider '{provider}'",
                       hint="Use one of: ollama, openai_compatible, heuristic")


def _check_ollama(settings: Settings) -> CheckResult:
    cfg = settings.llm.ollama
    try:
        resp = httpx.get(f"{cfg.base_url}/api/tags", timeout=5)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return CheckResult(
            "LLM provider",
            Status.FAIL,
            f"Ollama not reachable at {cfg.base_url}",
            hint=(
                f"{_short(exc)}\n"
                "      Install from https://ollama.com, then run it and pull a model:\n"
                f"      ollama pull {cfg.model}\n"
                "      Or run offline with: blossa scan --llm-provider heuristic"
            ),
        )
    models = [m.get("name", "") for m in resp.json().get("models", [])]
    if not _model_present(cfg.model, models):
        return CheckResult(
            "LLM provider",
            Status.WARN,
            f"Ollama up at {cfg.base_url}, but model '{cfg.model}' is not pulled",
            hint=f"ollama pull {cfg.model}",
        )
    return CheckResult("LLM provider", Status.OK, f"Ollama ({cfg.model}) reachable")


def _check_openai(settings: Settings) -> CheckResult:
    cfg = settings.llm.openai_compatible
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        resp = httpx.get(f"{cfg.base_url}/models", headers=headers, timeout=5)
    except httpx.HTTPError as exc:
        return CheckResult(
            "LLM provider",
            Status.FAIL,
            f"endpoint not reachable at {cfg.base_url}",
            hint=f"{_short(exc)}\n      Check base_url / api_key, or use --llm-provider heuristic",
        )
    if resp.status_code >= 500:
        return CheckResult("LLM provider", Status.FAIL,
                           f"endpoint returned {resp.status_code} at {cfg.base_url}")
    return CheckResult("LLM provider", Status.OK, f"OpenAI-compatible ({cfg.model}) reachable")


def check_output(settings: Settings) -> CheckResult:
    out_dir = Path(settings.output.dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".blossa_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("Output directory", Status.FAIL,
                           f"{out_dir} not writable", hint=_short(exc))
    return CheckResult("Output directory", Status.OK, f"{out_dir} writable")


# --------------------------------------------------------------------- runners


def run_diagnostics(settings: Settings, config_path: str | Path | None) -> Diagnosis:
    diag = Diagnosis()
    diag.add(check_python())
    diag.add(check_oracledb())
    diag.add(check_config(config_path))
    diag.add(check_oracle_connection(settings))
    diag.add(check_llm(settings))
    diag.add(check_output(settings))
    return diag


def llm_remediation(settings: Settings) -> str:
    """A focused, actionable message for when the LLM provider is unreachable at scan time."""
    result = check_llm(settings)
    lines = [result.detail]
    if result.hint:
        lines.append(result.hint.replace("      ", "  ").strip())
    return "\n".join(lines)


# --------------------------------------------------------------------- helpers


def _model_present(wanted: str, available: list[str]) -> bool:
    # Ollama reports e.g. "qwen2.5:14b"; accept a match with or without the ":latest" tag.
    base = wanted.split(":")[0]
    return any(m == wanted or m.split(":")[0] == base for m in available)


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__
