# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Detect application log / error / audit tables and the role each of their columns plays.

Companies routinely keep their own tables to record what happened to the business data — error
logs, change-audit trails, batch-job run logs. They are undocumented like everything else, but they
have a recognisable shape: a timestamp ("when"), a free-text message ("what went wrong"), usually a
severity/status, a source (the module/job that emitted it), an actor, and often a reference to the
business entity involved.

This module recognises them DETERMINISTICALLY — from the table name, the column shape (names +
types) and, when present, profiling signals. No LLM, no row values: it works the same offline as on
a live database, which is why detection lives here rather than in the semantic pass. The result is a
`LogTable` per table, tagging only the role-bearing columns so the rest of Blossa (the map, the
`ask` prompt, the Logs view) can reason about logs without re-deriving any of this.
"""

from __future__ import annotations

from .models import (
    ColumnInfo,
    ConfidenceLevel,
    LogColumn,
    LogKind,
    LogRole,
    LogTable,
    SchemaInfo,
    TableInfo,
)

# --------------------------------------------------------------- name signals

# Tokens (split on "_") that make a table NAME look like a log, plus a few run-together variants.
_LOGGY_TOKENS = {
    "LOG", "LOGS", "TRACE", "TRACES", "JOURNAL", "HISTORY", "HIST", "AUDIT", "AUDITS", "TRAIL",
    "EVENT", "EVENTS", "ACTIVITY", "ERR", "ERROR", "ERRORS", "EXCEPTION", "EXCEPTIONS", "FAULT",
    "MESSAGES", "CHANGELOG", "ERRORLOG", "AUDITLOG", "JOBLOG", "EVENTLOG", "SYSLOG", "RUNLOG",
}

# Name tokens that pin the KIND (checked in priority order: error → audit → job → event).
_KIND_TOKENS: list[tuple[LogKind, set[str]]] = [
    (LogKind.ERROR, {"ERR", "ERROR", "ERRORS", "EXCEPTION", "EXCEPTIONS", "FAULT", "ERRORLOG"}),
    (LogKind.AUDIT, {"AUDIT", "AUDITS", "TRAIL", "CHANGELOG", "AUDITLOG"}),
    (LogKind.JOB, {"JOB", "JOBS", "JOBLOG", "RUNLOG", "BATCH"}),
    (LogKind.EVENT, {"EVENT", "EVENTS", "ACTIVITY", "JOURNAL", "EVENTLOG"}),
]

# --------------------------------------------------------------- column-role signals

_TIME_WORDS = {
    "TIME", "TIMESTAMP", "TS", "DATE", "DATETIME", "LOGGED", "LOGTIME", "CREATED", "OCCURRED",
    "EVENT", "WHEN", "STARTED", "FINISHED", "ENDED", "START", "END", "AT",
}
_MESSAGE_WORDS = {
    "MESSAGE", "MSG", "TEXT", "DESCRIPTION", "DESC", "DETAIL", "DETAILS", "REASON", "NOTE", "NOTES",
    "COMMENT", "COMMENTS", "INFO", "STACK", "STACKTRACE", "EXCEPTION", "BODY", "CONTENT", "PAYLOAD",
    "ERRTEXT",
}
_SEVERITY_WORDS = {
    "SEVERITY", "LEVEL", "LOGLEVEL", "STATUS", "STATE", "OUTCOME", "RESULT", "PRIORITY",
}
_SOURCE_WORDS = {
    "MODULE", "SOURCE", "PROCEDURE", "PROC", "PROGRAM", "COMPONENT", "SERVICE", "OPERATION",
    "ACTION", "OBJECT", "JOB", "LOGGER", "CLASS", "METHOD", "CONTEXT", "CATEGORY", "FACILITY",
}
_ACTOR_WORDS = {"USER", "USERNAME", "DBUSER", "OSUSER", "ACTOR", "WHO", "LOGIN", "ACCOUNT"}
_CODE_WORDS = {"CODE", "SQLCODE", "ERRCODE", "ERRORCODE", "RC", "RETURNCODE", "STATUSCODE"}

_DATE_TYPES = {"DATE", "TIMESTAMP"}  # plus any "TIMESTAMP(…) WITH …" handled by startswith
_NUM_TYPES = {"NUMBER", "INTEGER", "INT", "FLOAT", "DECIMAL", "NUMERIC"}
_CHAR_TYPES = {"VARCHAR2", "NVARCHAR2", "VARCHAR", "CHAR", "NCHAR"}

# A VARCHAR this wide (or a CLOB) reads as free-text "message" even without a giveaway name.
_MESSAGE_MIN_LEN = 400


def _tokens(name: str) -> set[str]:
    return set(name.upper().split("_"))


def _is_date(col: ColumnInfo) -> bool:
    t = col.data_type.upper()
    return t in _DATE_TYPES or t.startswith("TIMESTAMP")


def _is_numeric(col: ColumnInfo) -> bool:
    return col.data_type.upper() in _NUM_TYPES


def _is_business_ref(col: ColumnInfo, table: TableInfo) -> bool:
    fk_cols = {c for fk in table.foreign_keys for c in fk.columns}
    if col.name in fk_cols:
        return True
    pk_cols = set(table.primary_key.columns) if table.primary_key else set()
    if col.name in pk_cols:
        return False
    name = col.name.upper()
    return name.endswith("_ID") or name.endswith("_NO") or name.endswith("_NUM")


def _role_for(col: ColumnInfo, table: TableInfo) -> LogRole | None:
    """Tag one column with the log role it plays, by keyword + type, then by shape; else None."""
    name = col.name.upper()
    toks = _tokens(name)

    if _is_date(col) and (toks & _TIME_WORDS or name.endswith(("_AT", "_TS"))):
        return LogRole.EVENT_TIME
    if _is_numeric(col) and toks & _CODE_WORDS:
        return LogRole.CODE
    if toks & _SEVERITY_WORDS:
        return LogRole.SEVERITY
    if toks & _ACTOR_WORDS or name.endswith("_BY"):
        return LogRole.ACTOR
    if toks & _SOURCE_WORDS:
        return LogRole.SOURCE
    if toks & _MESSAGE_WORDS:
        return LogRole.MESSAGE

    # Shape fallbacks (no giveaway name): a wide free-text column is the message; a bare
    # timestamp is the event time; an FK / *_ID is a business reference.
    t = col.data_type.upper()
    if t == "CLOB" or (t in _CHAR_TYPES and (col.data_length or 0) >= _MESSAGE_MIN_LEN):
        return LogRole.MESSAGE
    if _is_business_ref(col, table):
        return LogRole.BUSINESS_REF
    if _is_date(col):
        return LogRole.EVENT_TIME
    return None


def _kind_from_name(toks: set[str]) -> LogKind:
    for kind, kw in _KIND_TOKENS:
        if toks & kw:
            return kind
    return LogKind.GENERIC


def classify_table(table: TableInfo) -> LogTable | None:
    """Return a `LogTable` if `table` looks like an application log, else None.

    A table qualifies when it has the log shape (a timestamp + a free-text message), or when its
    name looks like a log and enough supporting role-columns are present. Confidence reflects how
    much of the name + shape agreed.
    """
    log_columns = [
        LogColumn(column=c.name, role=r) for c in table.columns if (r := _role_for(c, table))
    ]
    present = {lc.role for lc in log_columns}
    has_time = LogRole.EVENT_TIME in present
    has_message = LogRole.MESSAGE in present
    has_severity = LogRole.SEVERITY in present
    has_source = LogRole.SOURCE in present

    toks = _tokens(table.name)
    loggy = bool(toks & _LOGGY_TOKENS)

    score = sum((has_time, has_message, has_severity, has_source))
    is_log = (has_time and has_message) or (loggy and score >= 3)
    if not is_log:
        return None

    if loggy and has_time and has_message:
        confidence = ConfidenceLevel.HIGH
    elif (has_time and has_message) or (loggy and (has_time or has_message)):
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    return LogTable(
        table=table.name,
        owner=table.owner,
        kind=_kind_from_name(toks),
        confidence=confidence,
        evidence=_evidence(table, loggy, log_columns),
        columns=log_columns,
    )


def detect_log_tables(schema: SchemaInfo) -> list[LogTable]:
    """Find every application log / error / audit table in the schema (deterministic, no LLM)."""
    return [lt for t in schema.tables if (lt := classify_table(t))]


# ------------------------------------------------------------------- evidence


def _evidence(table: TableInfo, loggy: bool, log_columns: list[LogColumn]) -> list[str]:
    ev: list[str] = []
    if loggy:
        ev.append(f"table name '{table.name}' matches a log/error/audit pattern")
    by_role = {lc.role: lc.column for lc in log_columns}
    labels = {
        LogRole.EVENT_TIME: "timestamp",
        LogRole.MESSAGE: "free-text message",
        LogRole.SEVERITY: "severity/status",
        LogRole.SOURCE: "source/module",
        LogRole.ACTOR: "actor/user",
        LogRole.BUSINESS_REF: "business reference",
        LogRole.CODE: "error/status code",
    }
    for role, label in labels.items():
        if role in by_role:
            ev.append(f"has a {label} column ({by_role[role]})")
    return ev
