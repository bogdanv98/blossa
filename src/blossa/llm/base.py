# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""LLM provider interface + shared prompt building and response parsing.

A provider's one job is `analyze(summary) -> TableSemantics`. The prompt construction and the
JSON parsing are shared here so every model-backed provider behaves identically; only the
transport (how a prompt becomes text) differs per provider.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from ..models import (
    ColumnSemantics,
    ConfidenceLevel,
    TableSemantics,
    TableSummary,
)

SYSTEM_PROMPT = (
    "You are a database reverse-engineering assistant. You are given a PII-safe structural "
    "summary of a SINGLE database table from a legacy schema: its columns, types, keys, "
    "relationships, and aggregate value patterns (never raw data). Infer the likely BUSINESS "
    "meaning of the table and of each column.\n\n"
    "Rules:\n"
    "- Base every inference ONLY on the provided structure, names, comments and patterns.\n"
    "- Give each inference a confidence: 'high', 'medium', or 'low'.\n"
    "- Use 'low' when you are essentially guessing; say so plainly in the meaning text.\n"
    "- Cite the concrete evidence you used (a column name, a type, a relationship, a pattern).\n"
    "- Do NOT invent business facts that are not supported by the summary.\n"
    "- Respond with STRICT JSON only, no prose, no markdown fences."
)

_OUTPUT_CONTRACT = (
    'Respond with JSON of exactly this shape:\n'
    '{\n'
    '  "purpose": "<one or two sentences on what this table represents>",\n'
    '  "confidence": "high|medium|low",\n'
    '  "evidence": ["<short evidence string>", ...],\n'
    '  "columns": [\n'
    '    {"column": "<COLUMN_NAME>", "meaning": "<what it likely means>",\n'
    '     "confidence": "high|medium|low", "evidence": ["..."]}\n'
    '  ]\n'
    '}'
)


class LLMProvider(ABC):
    """Interface every provider implements."""

    name: str = "abstract"
    model: str | None = None

    @abstractmethod
    def analyze(self, summary: TableSummary) -> TableSemantics:
        """Return inferred semantics for one PII-safe table summary."""

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Free-form (JSON) generation, used by `blossa ask` for NL→SQL.

        Only model-backed providers implement this; the offline heuristic provider cannot
        translate natural language to SQL, so it raises.
        """
        raise NotImplementedError(
            f"The '{self.name}' provider cannot answer natural-language questions; "
            "use a model provider (ollama or openai_compatible)."
        )

    def available(self) -> bool:
        """Whether the provider is reachable / usable right now. Default: assume yes."""
        return True


# --------------------------------------------------------- prompt construction


def build_user_prompt(summary: TableSummary) -> str:
    """Render a compact, PII-safe JSON view of the table for the model."""
    payload = {
        "table": summary.name,
        "existing_comment": summary.comment,
        "approx_row_count": summary.row_count,
        "columns": [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "key_role": c.key_role.value,
                "existing_comment": c.comment,
                "references": c.references,
                "distinct_count": c.distinct_count,
                "null_fraction": c.null_fraction,
                "value_patterns": c.value_patterns,
                "masked_samples": c.masked_samples,
            }
            for c in summary.columns
        ],
        "references_out": [
            f"{r.from_table}.{','.join(r.from_columns)} -> {r.to_table}.{','.join(r.to_columns)}"
            f"{'' if r.declared else ' (inferred)'}"
            for r in summary.outbound
        ],
        "referenced_by_in": [
            f"{r.from_table}.{','.join(r.from_columns)} -> {r.to_table}.{','.join(r.to_columns)}"
            f"{'' if r.declared else ' (inferred)'}"
            for r in summary.inbound
        ],
    }
    return (
        f"Table summary (PII-safe JSON):\n{json.dumps(payload, indent=2, default=str)}\n\n"
        f"{_OUTPUT_CONTRACT}"
    )


# ------------------------------------------------------------ response parsing


def parse_response(summary: TableSummary, raw: str) -> TableSemantics:
    """Parse a model's JSON response into TableSemantics, defensively."""
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return _fallback_semantics(summary, "Model returned no parseable JSON.")

    columns = _parse_columns(summary, data.get("columns", []))
    return TableSemantics(
        table=summary.name,
        purpose=str(data.get("purpose") or "Purpose could not be inferred.").strip(),
        confidence=_coerce_confidence(data.get("confidence")),
        evidence=_as_str_list(data.get("evidence")),
        columns=columns,
    )


def _parse_columns(summary: TableSummary, raw_columns: object) -> list[ColumnSemantics]:
    known = {c.name.upper() for c in summary.columns}
    parsed: dict[str, ColumnSemantics] = {}
    if isinstance(raw_columns, list):
        for item in raw_columns:
            if not isinstance(item, dict):
                continue
            name = str(item.get("column", "")).upper()
            if name not in known:
                continue
            parsed[name] = ColumnSemantics(
                column=name,
                meaning=str(item.get("meaning") or "").strip() or "Meaning not inferred.",
                confidence=_coerce_confidence(item.get("confidence")),
                evidence=_as_str_list(item.get("evidence")),
            )
    # Ensure every column has an entry, even if the model skipped some.
    for col in summary.columns:
        parsed.setdefault(
            col.name.upper(),
            ColumnSemantics(
                column=col.name,
                meaning="Meaning not inferred by the model.",
                confidence=ConfidenceLevel.LOW,
                evidence=[],
            ),
        )
    # Preserve original column order.
    return [parsed[c.name.upper()] for c in summary.columns]


def _fallback_semantics(summary: TableSummary, reason: str) -> TableSemantics:
    return TableSemantics(
        table=summary.name,
        purpose=f"Purpose could not be inferred ({reason}).",
        confidence=ConfidenceLevel.LOW,
        evidence=[reason],
        columns=[
            ColumnSemantics(
                column=c.name,
                meaning="Meaning not inferred.",
                confidence=ConfidenceLevel.LOW,
            )
            for c in summary.columns
        ],
    )


def _loads_lenient(raw: str) -> object:
    text = raw.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fences if the model added them despite instructions.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # Fall back to the outermost {...} span if there is leading/trailing prose.
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
