# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Natural-language questions over a scanned schema (the `blossa ask` command).

The flow, and the trust/safety boundaries that make it usable for a non-technical analyst:

  question + database map (ScanReport)  ->  LLM  ->  ONE read-only SELECT (+ assumptions)

  * The LLM only ever sees the **semantic map** — table/column meanings + relationships — never
    raw row values, so the existing PII boundary holds.
  * The generated SQL is validated to be a single read-only SELECT before it touches the database,
    and the connection runs in a READ ONLY transaction regardless, so DML/DDL cannot execute.
  * The SQL is always shown to the user, with the model's assumptions and confidence, so the
    answer can be verified rather than trusted blindly.
  * Query results are returned to the user only; in this phase they are NOT fed back to the LLM,
    so no real data leaves for a model to read.

This module holds the pure, testable pieces (context, prompt, parsing, validation, row-limit);
the CLI wires them to a live provider + database.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from .models import ConfidenceLevel, ScanReport

# Keywords that must never appear in a query we are about to run. The READ ONLY transaction on the
# connection is the real backstop; this is defence-in-depth and gives a clearer error message.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|BEGIN|DECLARE|RENAME|COMMENT|FLASHBACK|INTO)\b",
    re.IGNORECASE,
)
_STARTS_SELECT = re.compile(r"^\s*\(*\s*(SELECT|WITH)\b", re.IGNORECASE)


class UnsafeQueryError(ValueError):
    """Raised when a generated query is not a single read-only SELECT."""


class AskResult(BaseModel):
    """What the model proposed for a natural-language question."""

    sql: str = ""
    explanation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW

    @property
    def answerable(self) -> bool:
        return bool(self.sql.strip())


ASK_SYSTEM_PROMPT = (
    "You are a careful data-analyst assistant. You translate a business user's natural-language "
    "question into exactly ONE read-only Oracle SQL query, using a provided semantic map of the "
    "database (tables, columns with inferred business meaning, and relationships).\n\n"
    "Rules:\n"
    "- Produce exactly ONE statement: a SELECT (a leading WITH ... SELECT is fine). NEVER write "
    "INSERT, UPDATE, DELETE, MERGE or any DDL.\n"
    "- Use ONLY tables and columns that appear in the map. Qualify columns when ambiguous.\n"
    "- Use the listed relationships for joins. Reference tables by the names shown in the map "
    "(owner-qualified when the name contains a dot).\n"
    "- Write standard Oracle SQL.\n"
    "- If the question cannot be answered from this schema, set \"sql\" to \"\" and explain why.\n"
    "- List every assumption you made (which column you picked, how you read a date or filter).\n"
    "- Respond with STRICT JSON only — no prose, no markdown fences."
)

_ASK_OUTPUT_CONTRACT = (
    "Respond with JSON of exactly this shape:\n"
    "{\n"
    '  "sql": "<a single read-only SELECT, or \\"\\" if unanswerable>",\n'
    '  "explanation": "<plain language: what the query returns, for a non-technical user>",\n'
    '  "assumptions": ["<assumption>", ...],\n'
    '  "confidence": "high|medium|low"\n'
    "}"
)


# --------------------------------------------------------------- schema context


def _distinct_owners(report: ScanReport) -> list[str]:
    owners: list[str] = []
    for t in report.schema_info.tables:
        if t.owner and t.owner not in owners:
            owners.append(t.owner)
    return owners


def _qualified(table_name: str, owner: str | None, multi_schema: bool) -> str:
    return f"{owner}.{table_name}" if (multi_schema and owner) else table_name


def build_schema_context(report: ScanReport) -> dict:
    """A compact, model-facing view of the map: tables + columns (with meaning) + relationships.

    No raw data — only structure and the inferred semantics already in the report.
    """
    multi = len(_distinct_owners(report)) > 1
    tables = []
    for table in report.schema_info.tables:
        sem = report.semantics_for(table.name)
        col_meaning = {c.column.upper(): c.meaning for c in sem.columns} if sem else {}
        pk_cols = set(table.primary_key.columns) if table.primary_key else set()
        fk_cols = {c for fk in table.foreign_keys for c in fk.columns}
        columns = [
            {
                "name": col.name,
                "type": col.type_signature,
                "key": "PK" if col.name in pk_cols else ("FK" if col.name in fk_cols else ""),
                "means": col_meaning.get(col.name.upper(), ""),
            }
            for col in table.columns
        ]
        tables.append(
            {
                "name": _qualified(table.name, table.owner, multi),
                "purpose": sem.purpose if sem else "",
                "columns": columns,
            }
        )

    relationships = []
    for r in report.relationships:
        src = _qualified(r.from_table, r.from_owner, multi)
        dst = _qualified(r.to_table, r.to_owner, multi)
        kind = "declared" if r.declared else f"inferred ({r.confidence.value})"
        relationships.append(
            f"{src}({', '.join(r.from_columns)}) -> {dst}({', '.join(r.to_columns)}) [{kind}]"
        )

    return {"schema": report.schema_info.name, "tables": tables, "relationships": relationships}


def build_ask_prompt(question: str, report: ScanReport) -> str:
    context = build_schema_context(report)
    return (
        f"Database map (semantic, PII-safe JSON):\n{json.dumps(context, indent=2, default=str)}\n\n"
        f"Business question:\n{question.strip()}\n\n"
        f"{_ASK_OUTPUT_CONTRACT}"
    )


# ------------------------------------------------------------- response parsing


def parse_ask_response(raw: str) -> AskResult:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return AskResult(explanation="The model did not return parseable JSON.")
    return AskResult(
        sql=str(data.get("sql") or "").strip(),
        explanation=str(data.get("explanation") or "").strip(),
        assumptions=_as_str_list(data.get("assumptions")),
        confidence=_coerce_confidence(data.get("confidence")),
    )


# ------------------------------------------------------------------- safety


def validate_read_only_select(sql: str) -> str:
    """Return the cleaned SQL if it is a single read-only SELECT, else raise UnsafeQueryError."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise UnsafeQueryError("No SQL was produced.")
    if ";" in s:
        raise UnsafeQueryError("Only a single statement is allowed (found ';' mid-query).")
    if not _STARTS_SELECT.match(s):
        raise UnsafeQueryError("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(s):
        raise UnsafeQueryError("Query contains a keyword that is not read-only.")
    return s


def with_row_limit(sql: str, max_rows: int) -> str:
    """Wrap a validated SELECT so at most `max_rows` rows come back (Oracle ROWNUM, order-safe)."""
    n = max(1, int(max_rows))
    return f"SELECT * FROM (\n{sql}\n) WHERE ROWNUM <= {n}"


# ------------------------------------------------------------------- helpers


def _loads_lenient(raw: str) -> object:
    text = raw.strip()
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
