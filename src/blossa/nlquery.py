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
  * Query results are returned to the user only; they are NOT fed back to the LLM, so no real
    data leaves for a model to read. This holds even for multi-turn refinement: a follow-up
    ("now break it down by year") carries back only the prior questions and the SQL the model
    itself produced — both structure/metadata, never a single row of data.

This module holds the pure, testable pieces (context, prompt, parsing, validation, row-limit);
the CLI wires them to a live provider + database.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from .logsense import ERROR_SEVERITIES
from .models import ConfidenceLevel, LogRole, ScanReport

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


class Turn(BaseModel):
    """One earlier exchange in a multi-turn `ask` conversation.

    Holds the user's question and the SQL the model produced for it (empty when that turn was
    answered in plain language, e.g. "what does this procedure do"). Deliberately carries NO query
    results — only the question text and the model's own SQL ever return to the model on a
    follow-up, so the "no raw rows to the LLM" boundary holds across turns.
    """

    question: str
    sql: str = ""


# Keep a follow-up prompt bounded: only the most recent turns are replayed to the model.
_MAX_HISTORY = 8


ASK_SYSTEM_PROMPT = (
    "You are a careful data-analyst assistant. You translate a business user's natural-language "
    "question into exactly ONE read-only Oracle SQL query.\n\n"
    "You are given a semantic map of the application schema(s) — tables, columns with inferred "
    "business meaning, relationships, a 'programs' list (stored procedures/functions/packages/"
    "triggers/views, each with a plain-language 'does' summary), and a 'log_tables' list "
    "(application log/error/audit tables, each column tagged with a role) — and a short 'Catalog "
    "views' list of Oracle data-dictionary views for questions about the database itself.\n\n"
    "Rules:\n"
    "- If the question asks what a procedure/function/package/trigger/view DOES, or about the "
    "application's logic, ANSWER IT IN PLAIN LANGUAGE from the 'programs' summaries in the map: "
    "put the answer in \"explanation\" and set \"sql\" to \"\" (there is no query to run). Name "
    "the units you describe. If the asked-about unit is not in the map, say so.\n"
    "- The question may be a FOLLOW-UP that refines the previous query in the conversation (e.g. "
    "'now break it down by year', 'only the top 5', 'add their email', 'exclude interns'). When it "
    "clearly builds on the last query, START FROM the most recent SQL shown in the conversation "
    "and adjust it — keep the earlier filters, joins and columns unless the user changed them. "
    "When the question is unrelated to the conversation, ignore the prior SQL and answer fresh.\n"
    "- Produce exactly ONE statement: a SELECT (a leading WITH ... SELECT is fine). NEVER write "
    "INSERT, UPDATE, DELETE, MERGE or any DDL.\n"
    "- For questions about the DATA, use ONLY tables and columns from the map. Qualify columns "
    "when ambiguous, use the listed relationships for joins, and reference tables by the names "
    "shown in the map (owner-qualified when the name contains a dot).\n"
    "- For questions about ERRORS, FAILURES, what went wrong, change-audit (who changed what) or "
    "job runs, use the 'log_tables'. Read each column's role: filter/sort by the 'event_time' "
    "column for recent entries or a time window; GROUP BY the 'severity' or 'source' column for "
    "'most common' / 'which module fails most'; show the 'message' column when the user wants to "
    "see the actual errors; and JOIN the 'business_ref' column back to its business table when "
    "asked which orders/customers/etc. were affected. Prefer the log table whose 'kind' matches "
    "(error / audit / job / event).\n"
    "- CRITICAL for error questions: a log table holds entries of EVERY severity, not just "
    "failures, EVEN when the table is named ERROR_LOG / *_LOG — its name does NOT mean every row "
    "is an error. So when the user asks for ERRORS or FAILURES (and not 'all entries' / "
    "'everything logged'), selecting from the table is NOT enough: you MUST add a filter on the "
    "'severity' column — UPPER(<severity>) IN ('ERROR','FATAL','SEVERE','CRITICAL','FAILED',"
    "'FAIL') — and never return informational 'INFO' (or 'WARN'/'WARNING') rows unless the user "
    "explicitly asks for warnings or for all entries. A date/time filter alone is wrong. "
    "Worked example — 'what errors happened today?' over a log with an event_time column LOG_TIME "
    "and a severity column SEVERITY becomes: WHERE TRUNC(LOG_TIME) = TRUNC(SYSDATE) AND "
    "UPPER(SEVERITY) IN ('ERROR','FATAL','SEVERE','CRITICAL','FAILED','FAIL').\n"
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

    programs = [
        {
            "name": _qualified(p.name, p.owner, multi),
            "kind": p.kind.value,
            "does": p.summary,
            "tables_used": p.tables_used,
        }
        for p in report.program_semantics
    ]

    log_tables = [
        {
            "name": _qualified(lt.table, lt.owner, multi),
            "kind": lt.kind.value,
            "columns": [{"name": c.column, "role": c.role.value} for c in lt.columns],
        }
        for lt in report.log_tables
    ]

    return {
        "schema": report.schema_info.name,
        "tables": tables,
        "relationships": relationships,
        "programs": programs,
        "log_tables": log_tables,
    }


def _history_block(history: list[Turn]) -> str:
    """Render the recent conversation (questions + the model's own SQL) for a follow-up prompt.

    Only the last `_MAX_HISTORY` turns are kept, so the prompt stays bounded. No query results are
    ever included — see `Turn`.
    """
    lines: list[str] = []
    for i, turn in enumerate(history[-_MAX_HISTORY:], start=1):
        lines.append(f"Q{i}: {turn.question.strip()}")
        sql = turn.sql.strip()
        lines.append(f"SQL{i}: {sql}" if sql else f"SQL{i}: (answered in plain language; no query)")
    return "\n".join(lines)


def build_ask_prompt(
    question: str,
    report: ScanReport,
    *,
    use_dba: bool = False,
    history: list[Turn] | None = None,
) -> str:
    context = json.dumps(build_schema_context(report), indent=2, default=str)
    catalog = catalog_reference(use_dba)
    convo = ""
    if history:
        convo = (
            "Conversation so far (earlier turns this session — the new question may refine the "
            "latest query; build on its SQL when it is a follow-up):\n"
            f"{_history_block(history)}\n\n"
        )
    return (
        f"Database map (semantic, PII-safe JSON):\n{context}\n\n"
        f"Catalog views (for questions about the database itself):\n{catalog}\n\n"
        f"{convo}"
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


# --------------------------------------------------- error-severity safety net

# A log table holds every severity, so "errors" must filter the severity column — but a model
# (especially over a large multi-schema map) often anchors on a table called ERROR_LOG and filters
# only by date. This deterministic net catches that case after generation, since prompt guidance
# alone is not reliable across models. English + Romanian, since questions come in either.
_ERROR_INTENT = (
    "error", "errors", "fail", "failed", "failure", "failing", "exception", "crash", "broke",
    "eroare", "erori", "esua", "eșua", "exceptie", "excepție", "picat", "cazut", "căzut",
)
# Phrases that mean the user explicitly wants non-error rows too — then we must NOT filter.
_ALL_OVERRIDE = (
    "all entries", "all severit", "every entry", "everything logged", "include info",
    "info", "warn", "warning", "toate intrar", "toate severit", "orice severit", "toate nivel",
)


def _is_error_intent(question: str) -> bool:
    q = question.lower()
    if any(w in q for w in _ALL_OVERRIDE):
        return False
    return any(w in q for w in _ERROR_INTENT)


def _already_filters_severity(sql: str, severity_col: str) -> bool:
    """True if the SQL already constrains the severity column (vs merely projecting it)."""
    if re.search(rf"\b{re.escape(severity_col)}\b\s*(=|!=|<>|\bIN\b|\bLIKE\b)", sql, re.IGNORECASE):
        return True
    upper = sql.upper()
    return any(f"'{lit}'" in upper for lit in (*ERROR_SEVERITIES, "INFO", "WARN", "WARNING"))


def _is_simple_select(sql: str) -> bool:
    """Only a single, un-nested SELECT is safe to inject a predicate into textually."""
    upper = sql.upper()
    if upper.count("SELECT") != 1:
        return False
    return not any(k in upper for k in (" UNION ", " MINUS ", " INTERSECT ", " HAVING "))


def _severity_predicate(severity_col: str) -> str:
    values = ", ".join(f"'{v}'" for v in ERROR_SEVERITIES)
    return f"UPPER({severity_col}) IN ({values})"


def _inject_severity_filter(sql: str, severity_col: str) -> str:
    """Add the error-severity predicate to a simple SELECT, before any GROUP BY/ORDER BY/FETCH."""
    upper = sql.upper()
    cuts = [i for kw in ("GROUP BY", "ORDER BY", "FETCH ") if (i := upper.find(kw)) != -1]
    cut = min(cuts) if cuts else len(sql)
    head, tail = sql[:cut].rstrip(), sql[cut:]
    pred = _severity_predicate(severity_col)
    head = f"{head} AND {pred}" if re.search(r"\bWHERE\b", head, re.IGNORECASE) else \
        f"{head} WHERE {pred}"
    return f"{head} {tail}".rstrip() if tail.strip() else head


def enforce_error_severity_filter(
    question: str, result: AskResult, report: ScanReport
) -> AskResult:
    """If an error question's SQL hits a log table but skips its severity filter, fix or flag it.

    Returns the (possibly updated) result: the severity filter is injected for a simple SELECT, or
    an assumption is appended warning that INFO/WARN rows may be included for a complex one. A
    deterministic backstop for the model under-filtering — independent of which model is used.
    """
    if not result.answerable or not _is_error_intent(question):
        return result
    for lt in report.log_tables:
        sev = lt.column_for(LogRole.SEVERITY)
        if not sev or not re.search(rf"\b{re.escape(lt.table)}\b", result.sql, re.IGNORECASE):
            continue
        if _already_filters_severity(result.sql, sev):
            return result
        if _is_simple_select(result.sql):
            result.sql = _inject_severity_filter(result.sql, sev)
            result.assumptions = [
                *result.assumptions,
                f"Restricted to error severities ({', '.join(ERROR_SEVERITIES)}) — ask for "
                "'all entries' to include INFO/WARN rows too.",
            ]
        else:
            result.assumptions = [
                *result.assumptions,
                f"Heads up: this did not filter {sev} to error levels, so INFO/WARN rows may be "
                f"included. Add UPPER({sev}) IN (...) or use `blossa logs` for errors only.",
            ]
        return result
    return result


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
