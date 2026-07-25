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
import re
from collections.abc import Callable

from .llm.base import LLMProvider, language_instruction
from .models import (
    ConfidenceLevel,
    ProgramKind,
    ProgramSemantics,
    ProgramUnit,
    RoutineSemantics,
)

ProgressFn = Callable[[str, int, int], None]

_SUBPROGRAM_RE = re.compile(
    r"^\s*(PROCEDURE|FUNCTION)\s+([A-Za-z][A-Za-z0-9_$#]*)", re.IGNORECASE | re.MULTILINE
)
_PACKAGE_BODY_RE = re.compile(r"\bPACKAGE\s+BODY\b", re.IGNORECASE)


def declared_subprograms(source: str) -> list[tuple[str, ProgramKind]]:
    """The routines a package DECLARES, as (NAME, PROCEDURE|FUNCTION), read out of its spec.

    A packaged subprogram is not a database object of its own — `CORE_BANKING.get_balance` never
    appears in ALL_OBJECTS, only its package does. Reading the spec is therefore the only way the
    map can know the callable exists, and without it a question like "is there a get_balance
    procedure?" can only be answered with a catalog query that is structurally unable to find one.

    Only the spec is scanned (the part before PACKAGE BODY), so package-private helpers — which a
    caller cannot invoke — are left out. Overloads collapse to one entry.
    """
    spec = _PACKAGE_BODY_RE.split(source or "", maxsplit=1)[0]
    found: list[tuple[str, ProgramKind]] = []
    seen: set[str] = set()
    for match in _SUBPROGRAM_RE.finditer(spec):
        name = match.group(2).upper()
        if name in seen:
            continue
        seen.add(name)
        kind = (
            ProgramKind.FUNCTION
            if match.group(1).upper() == "FUNCTION"
            else ProgramKind.PROCEDURE
        )
        found.append((name, kind))
    return found


def package_subprograms(source: str) -> list[str]:
    """Just the names from `declared_subprograms`, in declaration order."""
    return [name for name, _ in declared_subprograms(source)]


def routines_referencing(source: str, identifier: str) -> list[str]:
    """Which routines of a package mention `identifier` in their own section of the source.

    "The package uses LOANS" is true but coarse; the useful answer is WHICH routine does. The
    source is sectioned at each PROCEDURE/FUNCTION header (spec declarations and body
    implementations both count as sections — a spec section is just a signature, so a real
    reference can only surface in the body one) and each section is searched on a word boundary.
    Deterministic and offline; declaration order is preserved.
    """
    if not source or not identifier:
        return []
    pattern = re.compile(rf"\b{re.escape(identifier)}\b", re.IGNORECASE)
    headers = list(_SUBPROGRAM_RE.finditer(source))
    order: list[str] = []
    hit: set[str] = set()
    for i, header in enumerate(headers):
        name = header.group(2).upper()
        if name not in order:
            order.append(name)
        end = headers[i + 1].start() if i + 1 < len(headers) else len(source)
        if pattern.search(source, header.end(), end):
            hit.add(name)
    return [n for n in order if n in hit]

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
    '  "evidence": ["<short evidence string>", ...],\n'
    '  "routines": [{"name": "<ROUTINE_NAME>", "does": "<one sentence>"}, ...]\n'
    "}\n"
    'For a PACKAGE, fill "routines" with ONE SENTENCE per procedure/function the package '
    "declares (what that routine does for the business). For any other unit kind, return an "
    "empty routines list."
)


def trim_source(source: str) -> str:
    """Bound the source sent to the model, flagging the cut so it knows it saw a prefix."""
    s = source or ""
    if len(s) <= _MAX_SOURCE_CHARS:
        return s
    return s[:_MAX_SOURCE_CHARS] + "\n-- … source truncated for length …"


def build_program_prompt(
    unit: ProgramUnit, known_tables: list[str] | None = None, language: str | None = None
) -> str:
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
        f"{tables_hint}{language_instruction(language)}{_OUTPUT_CONTRACT}"
    )


def parse_program_response(unit: ProgramUnit, raw: str) -> ProgramSemantics:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return _fallback(unit, "The model returned no parseable JSON.")
    routines = [
        RoutineSemantics(name=str(r.get("name", "")).strip().upper(),
                         summary=str(r.get("does") or r.get("summary") or "").strip())
        for r in (data.get("routines") or [])
        if isinstance(r, dict) and str(r.get("name", "")).strip()
    ]
    return ProgramSemantics(
        name=unit.name,
        owner=unit.owner,
        kind=unit.kind,
        summary=str(data.get("summary") or "Purpose could not be inferred.").strip(),
        tables_used=_as_str_list(data.get("tables_used")),
        confidence=_coerce_confidence(data.get("confidence")),
        evidence=_as_str_list(data.get("evidence")),
        routines=routines,
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
            prompt = build_program_prompt(unit, known_tables, getattr(provider, "language", "en"))
            raw = provider.generate(PROGRAM_SYSTEM_PROMPT, prompt)
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
