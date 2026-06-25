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
    "question into exactly ONE read-only Oracle SQL query.\n\n"
    "You are given a semantic map of the application schema(s) — tables, columns with inferred "
    "business meaning, and relationships — and a short 'Catalog views' list of Oracle "
    "data-dictionary views for questions about the database itself.\n\n"
    "Rules:\n"
    "- Produce exactly ONE statement: a SELECT (a leading WITH ... SELECT is fine). NEVER write "
    "INSERT, UPDATE, DELETE, MERGE or any DDL.\n"
    "- For questions about the DATA, use ONLY tables and columns from the map. Qualify columns "
    "when ambiguous, use the listed relationships for joins, and reference tables by the names "
    "shown in the map (owner-qualified when the name contains a dot).\n"
    "- For questions about the DATABASE ITSELF (how many schemas, which tables exist, row counts, "
    "columns, constraints), use ONLY the views under 'Catalog views' below — exactly those view "
    "names, never another variant.\n"
    "- Write standard Oracle SQL.\n"
    "- If the question cannot be answered, set \"sql\" to \"\" and explain why.\n"
    "- List every assumption you made (which column you picked, how you read a date or filter).\n"
    "- Respond with STRICT JSON only — no prose, no markdown fences."
)

# ORACLE_MAINTAINED='N' is necessary but not sufficient to mean "application schema": a handful of
# Oracle operational accounts (the PDB admin, OS-authenticated OPS$ logins) also carry that flag yet
# hold no business data. This filter, applied to ALL_USERS/DBA_USERS, keeps only real app accounts.
# Mirrors the intent of introspect._SYSTEM_SCHEMAS on the scanning side.
_APP_OWNER_FILTER = (
    "WHERE ORACLE_MAINTAINED='N' AND USERNAME NOT LIKE 'OPS$%' AND USERNAME NOT IN ('PDBADMIN')"
)

# Catalog/metadata views, by scope. "scoped" uses ALL_* — Oracle limits these to objects this
# account may read, so answers are naturally confined to the granted schemas. "full" uses DBA_*
# (the whole database; needs SELECT_CATALOG_ROLE).
CATALOG_REFERENCE_SCOPED = (
    "- ALL_TABLES(OWNER, TABLE_NAME, NUM_ROWS): tables this account can read. NUM_ROWS is an "
    "approximate optimizer statistic (may be stale/NULL) — for an exact count of one table use "
    "COUNT(*).\n"
    "- ALL_TAB_COLUMNS(OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, NULLABLE): columns per table.\n"
    "- ALL_CONSTRAINTS(OWNER, TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, "
    "R_CONSTRAINT_NAME): 'P'=primary key, 'R'=foreign key, 'U'=unique, 'C'=check.\n"
    "- ALL_VIEWS(OWNER, VIEW_NAME): views this account can read.\n"
    "- ALL_OBJECTS(OWNER, OBJECT_NAME, OBJECT_TYPE): objects of EVERY kind (TABLE, VIEW, INDEX, "
    "SEQUENCE, PROCEDURE, TRIGGER, ...). To count one kind you MUST filter OBJECT_TYPE; without it "
    "you count all kinds at once.\n"
    "- ALL_USERS(USERNAME, ORACLE_MAINTAINED): use ONLY to tell application owners "
    "(ORACLE_MAINTAINED='N') from Oracle-internal ones; never list schemas from it directly — it "
    "shows every user, not just what this account can read.\n"
    "- ALL_TAB_COMMENTS / ALL_COL_COMMENTS: documentation comments.\n"
    "These ALL_* views show only objects this account may read, so answers are limited to the "
    "granted schemas — but public grants can still leak a few Oracle-internal objects, so ALWAYS "
    "restrict to application owners: keep only owners with ALL_USERS.ORACLE_MAINTAINED='N' (this "
    "drops SYS/SYSTEM/XDB and the like).\n"
    "Distinguish the two countings carefully — copy these exact patterns:\n"
    "  * How many/which TABLES (one row per table): "
    "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER IN "
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N'). "
    "Do NOT wrap this in DISTINCT OWNER — that would count schemas, not tables.\n"
    "  * How many/which SCHEMAS (one row per owner): "
    "SELECT COUNT(DISTINCT OWNER) FROM ALL_TABLES WHERE OWNER IN "
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N').\n"
    "  * How many/which VIEWS: SELECT COUNT(*) FROM ALL_VIEWS WHERE OWNER IN "
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N'). For another object kind, count "
    "ALL_OBJECTS with OBJECT_TYPE = that kind (e.g. 'INDEX', 'SEQUENCE', 'PROCEDURE', 'TRIGGER').\n"
    "  * Other Oracle object kinds have their own dictionary view — use it, filtered to "
    "application owners the same way, e.g. ALL_INDEXES, ALL_TRIGGERS, ALL_SEQUENCES, "
    "ALL_SYNONYMS, ALL_PROCEDURES, ALL_SCHEDULER_JOBS, ALL_SCHEDULER_CHAINS (a 'chain' is a "
    "DBMS_SCHEDULER job chain). If you do not recognise the object kind being asked about, set "
    "\"sql\" to \"\" and ask the user to clarify rather than guessing."
)
CATALOG_REFERENCE_FULL = (
    "- DBA_USERS(USERNAME, ORACLE_MAINTAINED, ACCOUNT_STATUS, CREATED): every user/schema. "
    "Application schemas have ORACLE_MAINTAINED = 'N' — exclude the others when counting apps.\n"
    "- DBA_TABLES(OWNER, TABLE_NAME, NUM_ROWS): all tables. NUM_ROWS is an approximate optimizer "
    "statistic (may be stale/NULL) — for an exact count of one table use COUNT(*).\n"
    "- DBA_TAB_COLUMNS(OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, NULLABLE): all columns.\n"
    "- DBA_CONSTRAINTS(OWNER, TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, "
    "R_CONSTRAINT_NAME): 'P'=primary key, 'R'=foreign key, 'U'=unique, 'C'=check.\n"
    "- DBA_VIEWS(OWNER, VIEW_NAME): all views.\n"
    "- DBA_OBJECTS(OWNER, OBJECT_NAME, OBJECT_TYPE): objects of EVERY kind (TABLE, VIEW, INDEX, "
    "SEQUENCE, PROCEDURE, TRIGGER, ...). To count one kind you MUST filter OBJECT_TYPE; without it "
    "you count all kinds at once.\n"
    "- DBA_TAB_COMMENTS / DBA_COL_COMMENTS: documentation comments.\n"
    "Distinguish the two countings carefully — copy these exact patterns:\n"
    "Application accounts are ORACLE_MAINTAINED='N', but a few Oracle operational accounts also "
    "carry that flag — exclude them everywhere with this filter:\n"
    f"    {_APP_OWNER_FILTER}\n"
    "  * How many/which TABLES (one row per table): "
    "SELECT COUNT(*) FROM DBA_TABLES WHERE OWNER IN "
    f"(SELECT USERNAME FROM DBA_USERS {_APP_OWNER_FILTER}). "
    "Do NOT wrap this in DISTINCT OWNER — that would count schemas, not tables.\n"
    "  * How many/which SCHEMAS (one row per owner, including app accounts with no tables yet): "
    f"SELECT COUNT(*) FROM DBA_USERS {_APP_OWNER_FILTER}.\n"
    "  * How many/which VIEWS: SELECT COUNT(*) FROM DBA_VIEWS WHERE OWNER IN "
    f"(SELECT USERNAME FROM DBA_USERS {_APP_OWNER_FILTER}). For another object kind, count "
    "DBA_OBJECTS with OBJECT_TYPE = that kind (e.g. 'INDEX', 'SEQUENCE', 'PROCEDURE', 'TRIGGER').\n"
    "  * Other Oracle object kinds have their own dictionary view — use it, filtered to "
    "application owners the same way, e.g. DBA_INDEXES, DBA_TRIGGERS, DBA_SEQUENCES, "
    "DBA_SYNONYMS, DBA_PROCEDURES, DBA_SCHEDULER_JOBS, DBA_SCHEDULER_CHAINS (a 'chain' is a "
    "DBMS_SCHEDULER job chain). If you do not recognise the object kind being asked about, set "
    "\"sql\" to \"\" and ask the user to clarify rather than guessing."
)


def catalog_reference(use_dba: bool) -> str:
    return CATALOG_REFERENCE_FULL if use_dba else CATALOG_REFERENCE_SCOPED


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


def build_ask_prompt(question: str, report: ScanReport, *, use_dba: bool = False) -> str:
    context = json.dumps(build_schema_context(report), indent=2, default=str)
    catalog = catalog_reference(use_dba)
    return (
        f"Database map (semantic, PII-safe JSON):\n{context}\n\n"
        f"Catalog views (for questions about the database itself):\n{catalog}\n\n"
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


def privilege_hint(sql: str, error: str) -> str | None:
    """If a catalog query failed for lack of privilege, suggest the fix; else None.

    A query over the whole-database DBA_* views needs SELECT_CATALOG_ROLE; without it Oracle
    reports ORA-00942 / insufficient privileges. Surface that as actionable guidance.
    """
    e = error.lower()
    denied = "ora-00942" in e or "insufficient priv" in e or "table or view does not exist" in e
    if "dba_" in sql.lower() and denied:
        return (
            "That catalog question used the whole-database DBA_* views, which need a privileged "
            "account (SELECT_CATALOG_ROLE). Use the 'full' access profile, or set "
            "oracle.catalog_scope: scoped to answer from the ALL_* views instead."
        )
    return None


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
