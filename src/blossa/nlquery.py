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
from .models import ConfidenceLevel, LogRole, ProgramKind, ScanReport
from .program import declared_subprograms, package_subprograms, routines_referencing

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
    "triggers/views, each with a plain-language 'does' summary, and for a package a 'contains' "
    "list of the procedures/functions it declares), and a 'log_tables' list "
    "(application log/error/audit tables, each column tagged with a role) — and a short 'Catalog "
    "views' list of Oracle data-dictionary views for questions about the database itself.\n\n"
    "Rules:\n"
    "- ANSWER IN THE USER'S LANGUAGE: write \"explanation\" and \"assumptions\" in the same "
    "language the question was asked in (a Romanian question gets a Romanian answer). Only the "
    "SQL itself stays in English/Oracle syntax.\n"
    "- If the question asks what a procedure/function/package/trigger/view DOES, or about the "
    "application's logic, ANSWER IT IN PLAIN LANGUAGE from the 'programs' summaries in the map: "
    "put the answer in \"explanation\" and set \"sql\" to \"\" (there is no query to run). Name "
    "the units you describe. If the asked-about unit is not in the map, say so.\n"
    "- If the question asks WHETHER a procedure/function EXISTS (e.g. 'is there a get_balance "
    "procedure?'), answer from the 'programs' list FIRST, checking each package's 'contains': a "
    "packaged subprogram is NOT an object in the catalog, so a query over *_OBJECTS can never find "
    "one and 'no rows' would wrongly read as 'it does not exist'. When it is in the map, set "
    "\"sql\" to \"\" and say where it lives (which package, which owner). Only if the map has no "
    "match, search the catalog with the *_PROCEDURES view described below.\n"
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
def _scheduler_reference(p: str, owner_subquery: str) -> str:
    """The DBMS_SCHEDULER views with their REAL columns, prefixed ALL_ or DBA_.

    Naming these views without their shape is exactly what made the model invent a
    `*_SCHEDULER_STEPS` view joined on a `CHAIN_ID` — neither exists. Every column below was read
    off the 21c dictionary, and the traps that caused that failure are called out inline.

    `owner_subquery` is the scope's own "which owners are applications" subquery. It is spelled
    out in a worked example because the second failure mode here was the model applying the
    ORACLE_MAINTAINED predicate straight to a scheduler view, which has no such column.
    """
    return (
        f"- {p}_SCHEDULER_JOBS(OWNER, JOB_NAME, JOB_SUBNAME, JOB_TYPE, JOB_ACTION, PROGRAM_NAME, "
        "SCHEDULE_TYPE, REPEAT_INTERVAL, ENABLED, STATE, RESTARTABLE, LAST_START_DATE, "
        "NEXT_RUN_DATE, RUN_COUNT, FAILURE_COUNT, COMMENTS): scheduled jobs. JOB_TYPE='CHAIN' "
        "means JOB_ACTION names the chain the job runs. ENABLED/RESTARTABLE are the STRINGS "
        "'TRUE'/'FALSE', not booleans. Rows with JOB_SUBNAME set are transient per-step runs the "
        "scheduler spawns, so add JOB_SUBNAME IS NULL to list the jobs a schema actually "
        "declares.\n"
        f"- {p}_SCHEDULER_CHAINS(OWNER, CHAIN_NAME, RULE_SET_OWNER, RULE_SET_NAME, "
        "NUMBER_OF_RULES, NUMBER_OF_STEPS, ENABLED, EVALUATION_INTERVAL, USER_RULE_SET, "
        "COMMENTS): job chains — a batch whose steps have an order. There is NO CHAIN_ID and NO "
        "SCHEMA_OWNER column: join chains to their steps and rules on OWNER + CHAIN_NAME.\n"
        f"- {p}_SCHEDULER_CHAIN_STEPS(OWNER, CHAIN_NAME, STEP_NAME, PROGRAM_OWNER, PROGRAM_NAME, "
        "STEP_TYPE, SKIP, PAUSE, PAUSE_BEFORE, TIMEOUT): the steps of a chain. The view is "
        f"_CHAIN_STEPS — there is no {p}_SCHEDULER_STEPS.\n"
        f"- {p}_SCHEDULER_CHAIN_RULES(OWNER, CHAIN_NAME, RULE_OWNER, RULE_NAME, CONDITION, "
        "ACTION, COMMENTS): the chain's edges, and the ONLY place its order is recorded — the "
        "steps do not carry it. CONDITION looks like 'S1 SUCCEEDED AND S2 SUCCEEDED', ACTION "
        "like 'START \"S3\"', 'START \"S3\",\"S4\"' (a parallel fan-out) or 'END 0'.\n"
        f"- {p}_SCHEDULER_PROGRAMS(OWNER, PROGRAM_NAME, PROGRAM_TYPE, PROGRAM_ACTION, "
        "NUMBER_OF_ARGUMENTS, ENABLED, COMMENTS): PROGRAM_ACTION is the procedure or block a "
        "chain step actually calls.\n"
        f"- {p}_SCHEDULER_JOB_RUN_DETAILS(OWNER, JOB_NAME, JOB_SUBNAME, LOG_ID, LOG_DATE, STATUS, "
        "ERROR#, ACTUAL_START_DATE, RUN_DURATION, ADDITIONAL_INFO): one row per execution. For a "
        "chain job the per-step rows carry STEP_NAME inside ADDITIONAL_INFO.\n"
        "  NONE of the scheduler views has an ORACLE_MAINTAINED column — that lives only on the "
        "users view — so restrict owners with a SUBQUERY, never with a predicate on the scheduler "
        "view itself. Worked example, every application chain with its steps:\n"
        f"    SELECT c.OWNER, c.CHAIN_NAME, s.STEP_NAME, s.PROGRAM_NAME\n"
        f"      FROM {p}_SCHEDULER_CHAINS c\n"
        f"      JOIN {p}_SCHEDULER_CHAIN_STEPS s\n"
        f"        ON s.OWNER = c.OWNER AND s.CHAIN_NAME = c.CHAIN_NAME\n"
        f"     WHERE c.OWNER IN {owner_subquery}\n"
        f"     ORDER BY c.OWNER, c.CHAIN_NAME, s.STEP_NAME\n"
        "  To show a chain's ORDER instead of just its steps, select CONDITION and ACTION from "
        f"{p}_SCHEDULER_CHAIN_RULES joined the same way.\n"
    )


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
    "you count all kinds at once. It lists only TOP-LEVEL objects: a procedure or function "
    "declared inside a package is NOT here (only the package is), so never search it by name.\n"
    "- ALL_PROCEDURES(OWNER, OBJECT_NAME, PROCEDURE_NAME): the callable subprograms. For a "
    "package, OBJECT_NAME is the package and PROCEDURE_NAME the routine inside it; for a "
    "standalone procedure/function, OBJECT_NAME is its name and PROCEDURE_NAME is NULL. This is "
    "the ONLY view "
    "that finds a packaged routine by name: WHERE PROCEDURE_NAME = 'GET_BALANCE'.\n"
    "- ALL_USERS(USERNAME, ORACLE_MAINTAINED): use ONLY to tell application owners "
    "(ORACLE_MAINTAINED='N') from Oracle-internal ones; never list schemas from it directly — it "
    "shows every user, not just what this account can read.\n"
    "- ALL_TAB_COMMENTS / ALL_COL_COMMENTS: documentation comments.\n"
    + _scheduler_reference(
        "ALL", "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N')"
    )
    + "These ALL_* views show only objects this account may read, so answers are limited to the "
    "granted schemas — but public grants can still leak a few Oracle-internal objects, so ALWAYS "
    "restrict to application owners: keep only owners with ALL_USERS.ORACLE_MAINTAINED='N' (this "
    "drops SYS/SYSTEM/XDB and the like).\n"
    "Distinguish the two countings carefully — copy these exact patterns:\n"
    "  * COUNT vs LIST — the patterns below COUNT. If the user asks WHICH ones, to LIST them, or "
    "whether any EXIST at all, select the identifying columns instead of COUNT(*) (e.g. SELECT "
    "OWNER, VIEW_NAME FROM ... ORDER BY 1, 2). A bare number answers 'how many', never 'which' or "
    "'is there any' — the user cannot see what was found.\n"
    "  * How many TABLES (one row per table): "
    "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER IN "
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N'). "
    "Do NOT wrap this in DISTINCT OWNER — that would count schemas, not tables.\n"
    "  * How many SCHEMAS (one row per owner): "
    "SELECT COUNT(DISTINCT OWNER) FROM ALL_TABLES WHERE OWNER IN "
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N').\n"
    "  * How many VIEWS: SELECT COUNT(*) FROM ALL_VIEWS WHERE OWNER IN"
    "(SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED='N'). For another object kind, count "
    "ALL_OBJECTS with OBJECT_TYPE = that kind (e.g. 'INDEX', 'SEQUENCE', 'PROCEDURE', 'TRIGGER').\n"
    "  * Other Oracle object kinds have their own dictionary view — use it, filtered to "
    "application owners the same way, e.g. ALL_INDEXES, ALL_TRIGGERS, ALL_SEQUENCES, "
    "ALL_SYNONYMS, ALL_PROCEDURES. For anything scheduled, use the ALL_SCHEDULER_* views "
    "documented above and their exact columns — do not invent a view name or a join key. If you "
    "do not recognise the object kind being asked about, set \"sql\" to \"\" and ask the user to "
    "clarify rather than guessing."
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
    "you count all kinds at once. It lists only TOP-LEVEL objects: a procedure or function "
    "declared inside a package is NOT here (only the package is), so never search it by name.\n"
    "- DBA_PROCEDURES(OWNER, OBJECT_NAME, PROCEDURE_NAME): the callable subprograms. For a "
    "package, OBJECT_NAME is the package and PROCEDURE_NAME the routine inside it; for a "
    "standalone procedure/function, OBJECT_NAME is its name and PROCEDURE_NAME is NULL. This is "
    "the ONLY view "
    "that finds a packaged routine by name: WHERE PROCEDURE_NAME = 'GET_BALANCE'.\n"
    "- DBA_TAB_COMMENTS / DBA_COL_COMMENTS: documentation comments.\n"
    + _scheduler_reference("DBA", f"(SELECT USERNAME FROM DBA_USERS {_APP_OWNER_FILTER})")
    + "Distinguish the two countings carefully — copy these exact patterns:\n"
    "Application accounts are ORACLE_MAINTAINED='N', but a few Oracle operational accounts also "
    "carry that flag — exclude them everywhere with this filter:\n"
    f"    {_APP_OWNER_FILTER}\n"
    "  * COUNT vs LIST — the patterns below COUNT. If the user asks WHICH ones, to LIST them, or "
    "whether any EXIST at all, select the identifying columns instead of COUNT(*) (e.g. SELECT "
    "OWNER, VIEW_NAME FROM ... ORDER BY 1, 2). A bare number answers 'how many', never 'which' or "
    "'is there any' — the user cannot see what was found.\n"
    "  * How many TABLES (one row per table): "
    "SELECT COUNT(*) FROM DBA_TABLES WHERE OWNER IN "
    f"(SELECT USERNAME FROM DBA_USERS {_APP_OWNER_FILTER}). "
    "Do NOT wrap this in DISTINCT OWNER — that would count schemas, not tables.\n"
    "  * How many SCHEMAS (one row per owner, including app accounts with no tables yet): "
    f"SELECT COUNT(*) FROM DBA_USERS {_APP_OWNER_FILTER}.\n"
    "  * How many VIEWS: SELECT COUNT(*) FROM DBA_VIEWS WHERE OWNER IN"
    f"(SELECT USERNAME FROM DBA_USERS {_APP_OWNER_FILTER}). For another object kind, count "
    "DBA_OBJECTS with OBJECT_TYPE = that kind (e.g. 'INDEX', 'SEQUENCE', 'PROCEDURE', 'TRIGGER').\n"
    "  * Other Oracle object kinds have their own dictionary view — use it, filtered to "
    "application owners the same way, e.g. DBA_INDEXES, DBA_TRIGGERS, DBA_SEQUENCES, "
    "DBA_SYNONYMS, DBA_PROCEDURES. For anything scheduled, use the DBA_SCHEDULER_* views "
    "documented above and their exact columns — do not invent a view name or a join key. If you "
    "do not recognise the object kind being asked about, set \"sql\" to \"\" and ask the user to "
    "clarify rather than guessing."
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

    # Driven by the captured units, not by the LLM's summaries: a unit the model never explained
    # (offline scan, or a failed explain) still has to be visible, or the model will conclude it
    # does not exist. For a package we also list what it CONTAINS — a packaged procedure/function
    # is not an object in the catalog, so the spec is the only place its name can come from.
    sem_by_unit = {
        (s.owner, s.name, s.kind): s for s in report.program_semantics
    }
    programs = []
    seen_units = set()
    for unit in report.schema_info.program_units:
        key = (unit.owner, unit.name, unit.kind)
        seen_units.add(key)
        sem = sem_by_unit.get(key)
        entry = {
            "name": _qualified(unit.name, unit.owner, multi),
            "kind": unit.kind.value,
            "does": sem.summary if sem else "",
            "tables_used": sem.tables_used if sem else [],
        }
        if unit.kind == ProgramKind.PACKAGE:
            contains = package_subprograms(unit.source)
            if contains:
                entry["contains"] = contains
        programs.append(entry)
    # A summary whose source was not captured (older map, or a unit read through a view the
    # account cannot see) still belongs in the context.
    programs.extend(
        {
            "name": _qualified(s.name, s.owner, multi),
            "kind": s.kind.value,
            "does": s.summary,
            "tables_used": s.tables_used,
        }
        for key, s in sem_by_unit.items()
        if key not in seen_units
    )

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
    # Naming the language explicitly, right next to the question, works where the generic
    # "answer in the user's language" rule in the system prompt does not: a small local model
    # follows a concrete instruction in the user turn far more reliably than a policy up top.
    language = (
        "Romanian (romana)" if _looks_romanian(question) else "the same language as the question"
    )
    return (
        f"Database map (semantic, PII-safe JSON):\n{context}\n\n"
        f"Catalog views (for questions about the database itself):\n{catalog}\n\n"
        f"{convo}"
        f"Business question:\n{question.strip()}\n\n"
        f"Write \"explanation\" and \"assumptions\" in {language}. The SQL itself stays in "
        "English/Oracle syntax.\n\n"
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


# --------------------------------------------------- program lookup safety net

# "Is there a get_balance procedure?" must be answered from the MAP, not with a catalog query:
# a packaged routine is not an object in ALL_OBJECTS, so a name search there returns zero rows and
# the user reads that as "it doesn't exist". The map already holds the answer (which package, which
# owner, and what the unit does), so we answer it directly and deterministically — prompt guidance
# alone did not hold. Only a POSITIVE match short-circuits: if the name is not in the map we say
# nothing and let the model query the catalog, since the map covers only the scanned schemas.

_LOOKUP_INTENT = (
    "exist", "is there", "are there", "do we have", "do you have", "where is", "where does",
    "which package", "what does", "what do", "find the", "look for",
    # "unde " keeps its trailing space: "undeva" (anywhere) must not read as "unde" (where).
    "exista", "există", "avem", "unde ", "ce face", "ce fac", "in ce pachet", "în ce pachet",
    "care pachet", "gaseste", "găsește", "cauta", "caută",
)

# "Is X USED anywhere?" is a different question from "does X exist?" — and answerable: the map
# carries every captured program source, and Oracle records object-level dependencies in
# ALL_/DBA_DEPENDENCIES. Answering it with "X exists, here is what it does" is a non-answer.
_USAGE_INTENT = (
    "used", "uses", "usage", "referenced", "references", "refers to", "depends on", "depend on",
    "calls", "called", "invoked", "invokes", "who uses",
    "foloseste", "folosește", "folosit", "folosita", "folosită", "utilizeaza", "utilizează",
    "utilizat", "apelat", "apeleaza", "apelează", "referit", "depinde",
)


def _is_usage_question(question: str) -> bool:
    return any(w in question.lower() for w in _USAGE_INTENT)

# Romanian markers. A diacritic or one unmistakably-Romanian word is enough; the weaker words
# (short, or shared with other languages) only count when at least two of them show up.
_RO_DIACRITICS = "ăâîșşțţ"
_RO_STRONG = frozenset({
    "exista", "unde", "avem", "pachetul", "procedura", "proceduri", "functia", "functie",
    "cate", "cati", "cine", "afla", "gaseste", "cauta", "despre",
})
_RO_WEAK = frozenset({"ce", "face", "fac", "care", "din", "sunt", "este", "cum", "se", "in", "si"})


def _looks_romanian(question: str) -> bool:
    q = question.lower()
    if any(ch in q for ch in _RO_DIACRITICS):
        return True
    words = set(re.findall(r"[a-z]+", q))
    if words & _RO_STRONG:
        return True
    return len(words & _RO_WEAK) >= 2


_KIND_RO = {
    ProgramKind.PROCEDURE: "procedură",
    ProgramKind.FUNCTION: "funcție",
    ProgramKind.PACKAGE: "pachet",
    ProgramKind.TRIGGER: "declanșator (trigger)",
    ProgramKind.VIEW: "vedere (view)",
}
_KIND_EN = {
    ProgramKind.PROCEDURE: "procedure",
    ProgramKind.FUNCTION: "function",
    ProgramKind.PACKAGE: "package",
    ProgramKind.TRIGGER: "trigger",
    ProgramKind.VIEW: "view",
}


class _ProgramHit(BaseModel):
    """One program unit (or packaged routine) the question named."""

    name: str
    kind: ProgramKind
    owner: str | None = None
    package: str | None = None  # set when the routine lives inside a package
    summary: str = ""


def _program_candidates(report: ScanReport) -> list[_ProgramHit]:
    sem = {(s.owner, s.name, s.kind): s.summary for s in report.program_semantics}
    hits: list[_ProgramHit] = []
    for unit in report.schema_info.program_units:
        summary = sem.get((unit.owner, unit.name, unit.kind), "")
        hits.append(
            _ProgramHit(name=unit.name.upper(), kind=unit.kind, owner=unit.owner, summary=summary)
        )
        if unit.kind == ProgramKind.PACKAGE:
            hits.extend(
                _ProgramHit(
                    name=routine,
                    kind=kind,
                    owner=unit.owner,
                    package=unit.name.upper(),
                    # The package's summary is the best description we have of a routine inside it.
                    summary=summary,
                )
                for routine, kind in declared_subprograms(unit.source)
            )
    return hits


def _qualify(owner: str | None, name: str) -> str:
    return f"{owner}.{name}" if owner else name


def _describe_hit(hit: _ProgramHit, romanian: bool) -> str:
    where = _qualify(hit.owner, hit.package) if hit.package else _qualify(hit.owner, hit.name)
    if romanian:
        kind = _KIND_RO[hit.kind]
        text = (
            f"Da — {hit.name} există: e o {kind} din pachetul {where}."
            if hit.package
            else f"Da — {hit.name} există: e o {kind} din schema {hit.owner or 'scanată'}."
        )
        if hit.summary:
            lead = "Pachetul din care face parte" if hit.package else "Ce face"
            text += f" {lead}: {hit.summary}"
        else:
            text += " Nu am un rezumat pentru el (scanarea a rulat fără model)."
        return text
    kind = _KIND_EN[hit.kind]
    text = (
        f"Yes — {hit.name} exists: it is a {kind} inside the package {where}."
        if hit.package
        else f"Yes — {hit.name} exists: it is a {kind} in {hit.owner or 'the scanned schema'}."
    )
    if hit.summary:
        lead = "Its package" if hit.package else "What it does"
        text += f" {lead}: {hit.summary}"
    else:
        text += " There is no summary for it (the scan ran without a model)."
    return text


def answer_program_lookup(
    question: str, report: ScanReport, use_dba: bool = False
) -> AskResult | None:
    """Answer "does X exist / where is X / is X used anywhere" from the map, or return None.

    Existence gets `sql=""` (the map is the answer); usage gets the units in the map whose source
    references the object, plus a query over the dependency catalog for objects the map cannot
    see. None when the question names nothing the map knows, so the model still gets its turn.
    Deterministic, so it holds whatever model is configured.
    """
    q = question.lower()
    usage = _is_usage_question(q)
    if not usage and not any(w in q for w in _LOOKUP_INTENT):
        return None
    tokens = {t.upper() for t in re.findall(r"[A-Za-z][A-Za-z0-9_$#]*", question)}
    if usage:
        return _answer_usage(question, tokens, report, use_dba)
    # Longest name first, so "CORE_BANKING.GET_BALANCE" prefers the routine over its package.
    matches = [h for h in _program_candidates(report) if h.name in tokens]
    if not matches:
        return None
    hit = max(matches, key=lambda h: (h.package is not None, len(h.name)))
    romanian = _looks_romanian(question)
    return AskResult(
        sql="",
        explanation=_describe_hit(hit, romanian),
        assumptions=[
            "Răspuns din harta scanată, fără a interoga baza de date."
            if romanian
            else "Answered from the scanned map, without querying the database."
        ],
        confidence=ConfidenceLevel.HIGH,
    )


def _answer_usage(
    question: str, tokens: set[str], report: ScanReport, use_dba: bool
) -> AskResult | None:
    """Who uses X: scan the captured sources, and query the dependency catalog when it can help.

    For a table, view or standalone program, ALL_/DBA_DEPENDENCIES is the authoritative answer
    (it sees objects outside the map), so it becomes the SQL. A packaged routine only ever
    appears in the catalog as its whole package, so there the source scan is the whole answer.
    """
    unit_hits = [h for h in _program_candidates(report) if h.name in tokens]
    table_hits = [t for t in report.schema_info.tables if t.name.upper() in tokens]
    if unit_hits:
        hit = max(unit_hits, key=lambda h: (h.package is not None, len(h.name)))
        name, owner, packaged = hit.name, hit.owner, hit.package is not None
        exclude = {name, hit.package or ""}
    elif table_hits:
        table = max(table_hits, key=lambda t: len(t.name))
        name, owner, packaged = table.name.upper(), table.owner, False
        exclude = {name}
    else:
        return None

    romanian = _looks_romanian(question)
    sem_by_unit = {(s.owner, s.name, s.kind): s for s in report.program_semantics}
    users = []
    for u in report.schema_info.program_units:
        if u.name.upper() in exclude:
            continue
        if not re.search(rf"\b{re.escape(name)}\b", u.source or "", re.IGNORECASE):
            continue
        label = f"{(u.owner + '.') if u.owner else ''}{u.name} ({u.kind.value.lower()})"
        sem = sem_by_unit.get((u.owner, u.name, u.kind))
        # For a package, name the ROUTINES that touch the object — "the package uses it" is
        # true but coarse — and say what each routine does when the scan captured that.
        if u.kind == ProgramKind.PACKAGE:
            details = []
            for routine in routines_referencing(u.source, name):
                # The sentence joins a list that ends with our own period — drop its final dot.
                does = (sem.routine_summary(routine) if sem else "").rstrip(".")
                details.append(f"{routine} — {does}" if does else routine)
            if details:
                inside = "în rutinele" if romanian else "in routines"
                label += f", {inside}: " + "; ".join(details)
        elif sem and sem.summary:
            label += f" — {sem.summary.rstrip('.')}"
        users.append(label)

    if users:
        found = (
            f"În codul capturat în hartă, {name} este folosit de: {'; '.join(users)}."
            if romanian
            else f"In the code captured in the map, {name} is used by: {'; '.join(users)}."
        )
    else:
        found = (
            f"{name} nu apare în niciun program capturat în hartă."
            if romanian
            else f"{name} is not referenced by any program captured in the map."
        )

    sql = ""
    if packaged:
        found += (
            " Fiind o rutină dintr-un pachet, catalogul Oracle urmărește dependențele doar la "
            "nivel de pachet întreg, deci sursa din hartă este răspunsul."
            if romanian
            else " Being a packaged routine, Oracle's dependency catalog only tracks the whole "
            "package, so the captured source is the answer."
        )
    else:
        # The tokens regex guarantees identifier-safe characters, so inlining is safe.
        view = "DBA_DEPENDENCIES" if use_dba else "ALL_DEPENDENCIES"
        owner_filter = f"\n   AND REFERENCED_OWNER = '{owner.upper()}'" if owner else ""
        sql = (
            f"SELECT OWNER, NAME, TYPE\n  FROM {view}\n"
            f" WHERE REFERENCED_NAME = '{name}'{owner_filter}\n ORDER BY OWNER, NAME"
        )
        found += (
            " Interogarea de mai jos verifică dependențele înregistrate în catalogul Oracle "
            "(prinde și obiecte din afara hărții)."
            if romanian
            else " The query below checks the dependencies Oracle itself recorded (it also "
            "catches objects outside the map)."
        )

    return AskResult(
        sql=sql,
        explanation=found,
        assumptions=[
            "Sursele din hartă au fost căutate textual; comentariile pot produce potriviri false."
            if romanian
            else "The captured sources were searched textually; a mention in a comment would "
            "also match."
        ],
        confidence=ConfidenceLevel.HIGH,
    )


# --------------------------------------------------- "which ones?" safety net

# "Are there any views?" answered with COUNT(*) = 1 tells the user a number and hides the answer.
# Prompt guidance did not hold (the model copies the counting pattern anyway), so this rewrites the
# projection deterministically: same query, same filters, but selecting the names it found.

_LIST_INTENT = (
    "which", "list ", "show me", "what are", "name them", "are there any", "is there any",
    "care sunt", "care este", "ce view", "exista", "există", "listeaz", "arata", "arată",
    "spune-mi care", "care anume",
)
# A counting question stays a counting question, even if it also contains a listing word.
_COUNT_INTENT = (
    "how many", "how much", "count of", "number of", "cate ", "câte ", "numar", "număr",
)

# Dictionary view -> the columns that identify a row in it. Only views we actually steer the model
# towards; anything else is left alone rather than guessed at.
_CATALOG_IDENTITY = {
    "VIEWS": "OWNER, VIEW_NAME",
    "TABLES": "OWNER, TABLE_NAME",
    "USERS": "USERNAME",
    "OBJECTS": "OWNER, OBJECT_NAME, OBJECT_TYPE",
    "PROCEDURES": "OWNER, OBJECT_NAME, PROCEDURE_NAME",
    "INDEXES": "OWNER, INDEX_NAME, TABLE_NAME",
    "SEQUENCES": "SEQUENCE_OWNER, SEQUENCE_NAME",
    "TRIGGERS": "OWNER, TRIGGER_NAME, TABLE_NAME",
    "SYNONYMS": "OWNER, SYNONYM_NAME",
    "CONSTRAINTS": "OWNER, CONSTRAINT_NAME, TABLE_NAME",
    "SCHEDULER_JOBS": "OWNER, JOB_NAME",
    "SCHEDULER_CHAINS": "OWNER, CHAIN_NAME",
}

_COUNT_SELECT = re.compile(r"^\s*SELECT\s+COUNT\s*\(\s*(DISTINCT\s+)?[^)]*\)\s+FROM\s+", re.I)
_CATALOG_FROM = re.compile(r"\bFROM\s+(?:ALL|DBA|USER)_([A-Z_]+)", re.I)


def _wants_a_list(question: str) -> bool:
    q = question.lower()
    if any(w in q for w in _COUNT_INTENT):
        return False
    return any(w in q for w in _LIST_INTENT)


def expand_count_to_list(question: str, result: AskResult) -> AskResult:
    """Turn `SELECT COUNT(*) FROM <catalog view>` into a listing when the user asked WHICH.

    Only the projection changes — every filter the model wrote is kept, so the rows returned are
    exactly the ones it was counting. Left untouched when the question is a counting one, when the
    query is not a simple catalog count, or when we have no identity columns for that view.
    """
    if not result.answerable or not _wants_a_list(question):
        return result
    sql = result.sql.strip()
    match = _COUNT_SELECT.match(sql)
    # The regex anchors on the OUTER projection, so a subquery in the WHERE clause is fine — only
    # a set operation would make "replace the select list" mean something else.
    if not match or any(
        op in sql.upper() for op in (" UNION ", " MINUS ", " INTERSECT ", " GROUP BY ")
    ):
        return result
    catalog = _CATALOG_FROM.search(sql)
    if not catalog:
        return result
    identity = _CATALOG_IDENTITY.get(catalog.group(1).upper())
    if not identity:
        return result
    if match.group(1):  # COUNT(DISTINCT owner) -> the distinct values themselves
        identity = f"DISTINCT {identity.split(',')[0].strip()}"
    rest = sql[match.end():]
    listed = f"SELECT {identity} FROM {rest}"
    if not re.search(r"\bORDER\s+BY\b", listed, re.IGNORECASE):
        listed = f"{listed}\nORDER BY 1"
    result.sql = listed
    romanian = _looks_romanian(question)
    result.assumptions = [
        *result.assumptions,
        "Am listat obiectele gasite, nu doar numarul lor - intreaba 'cate ...' pentru un total."
        if romanian
        else "Listed what was found rather than just counting it — ask 'how many …' for a total.",
    ]
    return result


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
        romanian = _looks_romanian(question)
        levels = ", ".join(ERROR_SEVERITIES)
        if _is_simple_select(result.sql):
            result.sql = _inject_severity_filter(result.sql, sev)
            note = (
                f"Limitat la severitatile de eroare ({levels}) — cere 'toate intrarile' ca sa "
                "incluzi si randurile INFO/WARN."
                if romanian
                else f"Restricted to error severities ({levels}) — ask for 'all entries' to "
                "include INFO/WARN rows too."
            )
        else:
            note = (
                f"Atentie: interogarea nu a filtrat {sev} la nivelurile de eroare, deci pot "
                f"aparea si randuri INFO/WARN. Adauga UPPER({sev}) IN (...) sau foloseste "
                "`blossa logs` doar pentru erori."
                if romanian
                else f"Heads up: this did not filter {sev} to error levels, so INFO/WARN rows may "
                f"be included. Add UPPER({sev}) IN (...) or use `blossa logs` for errors only."
            )
        result.assumptions = [*result.assumptions, note]
        return result
    return result


# A dictionary view referenced in a generated query, e.g. DBA_SCHEDULER_CHAINS.
_DICT_VIEW = re.compile(r"\b((?:DBA|ALL|USER)_[A-Z0-9_$#]+)\b", re.IGNORECASE)

# The scheduler family is where a model most often invents a plausible-looking view, because the
# real names are irregular (_CHAIN_STEPS, not _STEPS). Knowing the true set lets us say "no such
# view" with certainty instead of guessing at privileges.
_SCHEDULER_VIEWS = frozenset(
    f"{prefix}_SCHEDULER_{suffix}"
    for prefix in ("DBA", "ALL", "USER")
    for suffix in (
        "JOBS", "CHAINS", "CHAIN_STEPS", "CHAIN_RULES", "PROGRAMS", "PROGRAM_ARGS",
        "JOB_ARGS", "JOB_CLASSES", "JOB_LOG", "JOB_RUN_DETAILS", "RUNNING_JOBS",
        "SCHEDULES", "WINDOWS", "GLOBAL_ATTRIBUTE", "CREDENTIALS", "FILE_WATCHERS",
        "DESTINATIONS", "NOTIFICATIONS", "GROUPS", "JOB_DESTS", "CHAIN_RULES_TRANSLATED",
    )
)


def unknown_dictionary_views(sql: str) -> list[str]:
    """Dictionary views in `sql` that provably do not exist in Oracle.

    Only the scheduler family is checked, because it is the only one whose full membership we
    know. Everything else is left alone rather than risk calling a real view imaginary.
    """
    seen: list[str] = []
    for name in _DICT_VIEW.findall(sql):
        upper = name.upper()
        if "_SCHEDULER_" not in upper or upper in _SCHEDULER_VIEWS or upper in seen:
            continue
        seen.append(upper)
    return seen


def privilege_hint(sql: str, error: str, catalog_readable: bool | None = None) -> str | None:
    """Explain a failed catalog query, or None when the failure is something else.

    ORA-00942 says "table or view does not exist", and Oracle returns it for TWO different
    problems: the view really is missing (a wrong name), or it exists but this account cannot see
    it (a missing privilege). Those have opposite fixes, so guessing sends the reader the wrong
    way — this used to always blame privileges, which is wrong whenever the model simply invented
    a view name.

    `catalog_readable` is the caller's probe of whether the account can read DBA_* at all. When it
    is True the privilege explanation is ruled out; when it is None we do not know, and say so.
    """
    e = error.lower()
    denied = "ora-00942" in e or "insufficient priv" in e or "table or view does not exist" in e
    if not denied:
        return None

    bogus = unknown_dictionary_views(sql)
    if bogus:
        return (
            f"No such view: {', '.join(bogus)}. The scheduler views are ALL_/DBA_SCHEDULER_JOBS, "
            "_CHAINS, _CHAIN_STEPS, _CHAIN_RULES, _PROGRAMS and _JOB_RUN_DETAILS — chains join to "
            "their steps and rules on OWNER + CHAIN_NAME, and there is no chain id. Rephrase the "
            "question and Blossa will retry with the documented shape."
        )

    if "dba_" not in sql.lower():
        return None

    if catalog_readable:
        return (
            "This account can read the DBA_* views, so it is not a privilege problem: one of the "
            "names in the query does not exist. Check the view and column names against the "
            "catalog list Blossa provided."
        )
    if catalog_readable is False:
        return (
            "That catalog question used the whole-database DBA_* views, which need a privileged "
            "account (SELECT_CATALOG_ROLE). Use the 'full' access profile, or set "
            "oracle.catalog_scope: scoped to answer from the ALL_* views instead."
        )
    return (
        "That catalog question used the whole-database DBA_* views. Either the account lacks "
        "SELECT_CATALOG_ROLE (use the 'full' access profile, or set oracle.catalog_scope: scoped "
        "to answer from the ALL_* views), or one of the view names does not exist."
    )


_CATALOG_PROBE_SQL = "SELECT 1 FROM DBA_OBJECTS WHERE ROWNUM = 1"


def catalog_is_readable(db: object) -> bool | None:
    """Whether this account can read the DBA_* catalog at all — the discriminator for ORA-00942.

    Returns None if the probe itself could not be attempted, so the caller can say "unknown"
    rather than assert either cause.
    """
    query = getattr(db, "query", None)
    if query is None:
        return None
    try:
        query(_CATALOG_PROBE_SQL)
    except Exception:  # noqa: BLE001 - a refusal IS the answer: no catalog access
        return False
    return True


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
