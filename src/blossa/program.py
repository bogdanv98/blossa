# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Explain stored program units (procedures/functions/packages/triggers/views) with the LLM.

This is the code-understanding counterpart to the table semantic pass: the model reads a unit's
SOURCE and returns a plain-language read of what it does and the business logic behind it.

Trust boundary: a unit's source is PL/SQL or a view's defining SELECT — i.e. DDL/metadata, NOT
row data. Sending it to the model is allowed under the same rule that already sends table and
column structure; no raw row values are ever involved.

The pure pieces (prompt, parsing, source trimming) live here so they're testable without a model;
`run_program_pass` drives a live provider over many units, degrading one failure to a labelled
low-confidence result rather than aborting the scan.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .llm.base import LLMProvider
from .models import ConfidenceLevel, ProgramSemantics, ProgramUnit

ProgressFn = Callable[[str, int, int], None]

# Cap how much source we send per unit. Enough for the logic of normal app code; keeps the prompt
# bounded for a pathological generated/wrapped unit. The model is told when it was truncated.
_MAX_SOURCE_CHARS = 8000

PROGRAM_SYSTEM_PROMPT = (
    "You are a senior developer reverse-engineering an undocumented database application. You are "
    "given the SOURCE of ONE stored program unit (a PL/SQL procedure, function, package or "
    "trigger, or a view's defining query). Explain, for a business reader, what it does and the "
    "business logic behind it.\n\n"
    "Rules:\n"
    "- Base everything ONLY on the given source — never invent behaviour you cannot see.\n"
    "- Write the summary in plain language a business analyst can follow; lead with the WHAT "
    "(its purpose / the rule it enforces), then the key steps if useful.\n"
    "- List the application tables/views it reads or writes, by name, in 'tables_used'.\n"
    "- Give a confidence: 'high', 'medium' or 'low' (use 'low' for wrapped/obfuscated or unclear "
    "code, and say so).\n"
    "- Cite concrete evidence (a statement, a name, a condition).\n"
    "- Respond with STRICT JSON only — no prose, no markdown fences."
)

_OUTPUT_CONTRACT = (
    "Respond with JSON of exactly this shape:\n"
    "{\n"
    '  "summary": "<what the unit does and the business logic, in plain language>",\n'
    '  "tables_used": ["<TABLE_OR_VIEW>", ...],\n'
    '  "confidence": "high|medium|low",\n'
    '  "evidence": ["<short evidence string>", ...]\n'
    "}"
)


def trim_source(source: str) -> str:
    """Bound the source sent to the model, flagging the cut so it knows it saw a prefix."""
    s = source or ""
    if len(s) <= _MAX_SOURCE_CHARS:
        return s
    return s[:_MAX_SOURCE_CHARS] + "\n-- … source truncated for length …"


def build_program_prompt(unit: ProgramUnit, known_tables: list[str] | None = None) -> str:
    payload = {
        "name": unit.name,
        "owner": unit.owner,
        "kind": unit.kind.value,
        "source": trim_source(unit.source),
    }
    tables_hint = ""
    if known_tables:
        tables_hint = (
            "Application tables in this database (use these exact names in tables_used when the "
            f"source touches them):\n{', '.join(known_tables)}\n\n"
        )
    return (
        f"Program unit source (DDL/metadata, not row data):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        f"{tables_hint}{_OUTPUT_CONTRACT}"
    )


def parse_program_response(unit: ProgramUnit, raw: str) -> ProgramSemantics:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return _fallback(unit, "The model returned no parseable JSON.")
    return ProgramSemantics(
        name=unit.name,
        owner=unit.owner,
        kind=unit.kind,
        summary=str(data.get("summary") or "Purpose could not be inferred.").strip(),
        tables_used=_as_str_list(data.get("tables_used")),
        confidence=_coerce_confidence(data.get("confidence")),
        evidence=_as_str_list(data.get("evidence")),
    )


def run_program_pass(
    provider: LLMProvider,
    units: list[ProgramUnit],
    known_tables: list[str] | None = None,
    progress: ProgressFn | None = None,
) -> list[ProgramSemantics]:
    results: list[ProgramSemantics] = []
    total = len(units)
    for i, unit in enumerate(units, start=1):
        if progress:
            progress(unit.name, i, total)
        try:
            raw = provider.generate(PROGRAM_SYSTEM_PROMPT, build_program_prompt(unit, known_tables))
            results.append(parse_program_response(unit, raw))
        except Exception as exc:  # noqa: BLE001 - one unit's failure must not kill the scan
            results.append(_fallback(unit, f"{type(exc).__name__}: {exc}"))
    return results


# ------------------------------------------------------------------- helpers


def _fallback(unit: ProgramUnit, reason: str) -> ProgramSemantics:
    return ProgramSemantics(
        name=unit.name,
        owner=unit.owner,
        kind=unit.kind,
        summary=f"Could not explain this {unit.kind.value.lower()} ({reason}).",
        confidence=ConfidenceLevel.LOW,
        evidence=[reason],
    )


def _loads_lenient(raw: str) -> object:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _coerce_confidence(value: object) -> ConfidenceLevel:
    try:
        return ConfidenceLevel(str(value).strip().lower())
    except ValueError:
        return ConfidenceLevel.LOW


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
