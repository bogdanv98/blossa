# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""The CREATE statement behind any database object — what a DBA opens an IDE to read.

Two sources, tried in order:

1. **Oracle itself** — `DBMS_METADATA.GET_DDL`, the authoritative text (storage clauses,
   constraints, everything). It needs the privilege to see the object; for another schema's
   objects that means the full catalog profile.
2. **What the scan already captured** — a fallback so the workspace still shows something
   useful when DBMS_METADATA is unavailable: the captured PL/SQL / view source for program
   units, and a CREATE TABLE synthesized from the map for tables.

DDL is structure, not row data, so it falls on the same side of the boundary as the rest of the
map: safe to show and (like program source already is) safe to reason about.
"""

from __future__ import annotations

import re
from typing import Any

from .models import ConstraintType, ScanReport, TableInfo

# Oracle OBJECT_TYPE -> the object_type argument DBMS_METADATA.GET_DDL expects (it wants
# underscores where the catalog has spaces). Anything not listed here has no DDL view here.
METADATA_TYPES = {
    "TABLE": "TABLE",
    "VIEW": "VIEW",
    "PACKAGE": "PACKAGE",
    "PROCEDURE": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "TRIGGER": "TRIGGER",
    "SEQUENCE": "SEQUENCE",
    "SYNONYM": "SYNONYM",
    "MATERIALIZED VIEW": "MATERIALIZED_VIEW",
    "TYPE": "TYPE",
    "INDEX": "INDEX",
    # Every DBMS_SCHEDULER object is fetched under the single type PROCOBJ, not under its own
    # catalog name. GET_DDL then returns the anonymous block that recreates it — for a chain,
    # the create_chain + define_chain_step + define_chain_rule script in full.
    "JOB": "PROCOBJ",
    "PROGRAM": "PROCOBJ",
    "CHAIN": "PROCOBJ",
    "SCHEDULE": "PROCOBJ",
    "JOB CLASS": "PROCOBJ",
}

# GET_DDL returns a CLOB; the caller's row reader turns it into text. Everything is bound, so no
# identifier is ever concatenated into this statement.
GET_DDL_SQL = "SELECT DBMS_METADATA.GET_DDL(:otype, :name, :owner) AS DDL FROM DUAL"

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def metadata_type(object_type: str) -> str | None:
    """The DBMS_METADATA type for an Oracle OBJECT_TYPE, or None if we don't fetch DDL for it."""
    return METADATA_TYPES.get((object_type or "").strip().upper())


def split_qualified(name: str) -> tuple[str | None, str]:
    """Split "OWNER.OBJECT" into its parts; a bare name yields (None, name).

    The UI qualifies names with the owner only in multi-schema maps, so both forms arrive here.
    """
    text = (name or "").strip().strip('"')
    if "." in text:
        owner, _, obj = text.partition(".")
        return owner.strip().upper() or None, obj.strip().upper()
    return None, text.upper()


def validate_identifier(name: str, kind: str = "object") -> str:
    """Validate and upper-case an Oracle identifier, or raise ValueError."""
    n = (name or "").strip().upper()
    if not _IDENT.match(n):
        raise ValueError(f"Invalid {kind} name: {name!r}.")
    return n


def clob_text(value: Any) -> str:
    """GET_DDL hands back a CLOB; read it into plain text."""
    if value is None:
        return ""
    if hasattr(value, "read"):
        return str(value.read())
    return str(value)


def offline_ddl(report: ScanReport, owner: str | None, name: str, object_type: str) -> str:
    """DDL assembled from the scan alone — used when the database won't hand its own over.

    Program units keep the source we captured verbatim; a table is reconstructed from the map.
    Returns "" when the map has nothing for that object.
    """
    kind = (object_type or "").strip().upper()
    if kind == "TABLE":
        table = _find_table(report, owner, name)
        return synthesize_table_ddl(table) if table else ""
    if kind == "CHAIN":
        chain = next(
            (
                c
                for c in report.schema_info.scheduler_chains
                if c.name.upper() == name.upper()
                and (not owner or (c.owner or "").upper() == owner)
            ),
            None,
        )
        return synthesize_chain_ddl(chain) if chain else ""
    if kind == "JOB":
        job = next(
            (
                j
                for j in report.schema_info.scheduler_jobs
                if j.name.upper() == name.upper()
                and (not owner or (j.owner or "").upper() == owner)
            ),
            None,
        )
        return synthesize_job_ddl(job) if job else ""
    unit = next(
        (
            u
            for u in report.schema_info.program_units
            if u.name.upper() == name.upper() and (not owner or (u.owner or "").upper() == owner)
        ),
        None,
    )
    return (unit.source or "").strip() if unit else ""


def synthesize_chain_ddl(chain) -> str:
    """Rebuild a job chain as the DBMS_SCHEDULER calls that would recreate it.

    The offline counterpart to GET_DDL for a chain. Rules come last and in name order, which is
    how they read as a graph: the START rule first, the END rules last.
    """
    qualified = f"{chain.owner}.{chain.name}" if chain.owner else chain.name
    lines = [
        "-- Reconstructed by Blossa from the scanned map (Oracle's own DDL was unavailable).",
        "BEGIN",
        f"  DBMS_SCHEDULER.CREATE_CHAIN(chain_name => {_quote(qualified)}"
        + (f",\n{' ' * 22}comments   => {_quote(chain.comment)}" if chain.comment else "")
        + ");",
        "",
    ]
    for step in chain.steps:
        program = step.program_name or ""
        if step.program_owner and program:
            program = f"{step.program_owner}.{program}"
        lines.append(
            f"  DBMS_SCHEDULER.DEFINE_CHAIN_STEP({_quote(qualified)}, "
            f"{_quote(step.name)}, {_quote(program)});"
            + (f"   -- calls {step.action}" if step.action else "")
        )
    if chain.steps:
        lines.append("")
    for rule in chain.rules:
        lines.append(
            f"  DBMS_SCHEDULER.DEFINE_CHAIN_RULE({_quote(qualified)},\n"
            f"    condition => {_quote(rule.condition)},\n"
            f"    action    => {_quote(rule.action)},\n"
            f"    rule_name => {_quote(rule.name)});"
        )
    if chain.enabled:
        lines.append("")
        lines.append(f"  DBMS_SCHEDULER.ENABLE({_quote(qualified)});")
    lines.append("END;")
    lines.append("/")
    return "\n".join(lines)


def synthesize_job_ddl(job) -> str:
    """Rebuild a scheduled job as the DBMS_SCHEDULER call that would recreate it."""
    qualified = f"{job.owner}.{job.name}" if job.owner else job.name
    args = [f"job_name        => {_quote(qualified)}"]
    if job.job_type:
        args.append(f"job_type        => {_quote(job.job_type)}")
    if job.job_action:
        args.append(f"job_action      => {_quote(job.job_action)}")
    if job.program_name:
        args.append(f"program_name    => {_quote(job.program_name)}")
    if job.repeat_interval:
        args.append(f"repeat_interval => {_quote(job.repeat_interval)}")
    args.append(f"enabled         => {'TRUE' if job.enabled else 'FALSE'}")
    if job.comment:
        args.append(f"comments        => {_quote(job.comment)}")

    lines = [
        "-- Reconstructed by Blossa from the scanned map (Oracle's own DDL was unavailable).",
        "BEGIN",
        "  DBMS_SCHEDULER.CREATE_JOB(",
        ",\n".join(f"    {a}" for a in args),
        "  );",
    ]
    if job.restartable:
        lines.append(
            f"  DBMS_SCHEDULER.SET_ATTRIBUTE({_quote(qualified)}, 'restartable', TRUE);"
        )
    lines.append("END;")
    lines.append("/")
    return "\n".join(lines)


def _find_table(report: ScanReport, owner: str | None, name: str) -> TableInfo | None:
    return next(
        (
            t
            for t in report.schema_info.tables
            if t.name.upper() == name.upper() and (not owner or (t.owner or "").upper() == owner)
        ),
        None,
    )


def synthesize_table_ddl(table: TableInfo) -> str:
    """Build a readable CREATE TABLE from the introspected map (the offline fallback).

    Faithful to what the scan captured — columns, defaults, nullability, keys, indexes and
    comments — but it is a reconstruction, not Oracle's own text: no storage, partitioning or
    tablespace clauses. The header says so, so nobody ships it as a migration by mistake.
    """
    qualified = f"{table.owner}.{table.name}" if table.owner else table.name
    lines = [
        "-- Reconstructed by Blossa from the scanned map (no storage/partitioning clauses).",
        f"CREATE TABLE {qualified} (",
    ]

    body: list[str] = []
    width = max((len(c.name) for c in table.columns), default=0)
    for col in table.columns:
        piece = f"  {col.name.ljust(width)}  {col.type_signature}"
        if col.data_default:
            piece += f" DEFAULT {col.data_default}"
        if not col.nullable:
            piece += " NOT NULL"
        body.append(piece)

    for cons in table.constraints:
        rendered = _render_constraint(cons)
        if rendered:
            body.append(f"  CONSTRAINT {cons.name} {rendered}")

    lines.append(",\n".join(body))
    lines.append(");")

    for idx in table.indexes:
        if _index_backs_a_constraint(idx.name, table):
            continue  # the constraint above already creates it
        unique = "UNIQUE " if idx.unique else ""
        cols = ", ".join(idx.columns)
        lines.append(f"CREATE {unique}INDEX {idx.name} ON {qualified} ({cols});")

    if table.comment:
        lines.append(f"COMMENT ON TABLE {qualified} IS {_quote(table.comment)};")
    for col in table.columns:
        if col.comment:
            lines.append(f"COMMENT ON COLUMN {qualified}.{col.name} IS {_quote(col.comment)};")

    return "\n".join(lines)


def _render_constraint(cons) -> str:
    cols = ", ".join(cons.columns)
    if cons.type == ConstraintType.PRIMARY_KEY:
        return f"PRIMARY KEY ({cols})"
    if cons.type == ConstraintType.UNIQUE:
        return f"UNIQUE ({cols})"
    if cons.type == ConstraintType.FOREIGN_KEY and cons.referenced_table:
        ref_cols = ", ".join(cons.referenced_columns)
        target = f"{cons.referenced_table} ({ref_cols})" if ref_cols else cons.referenced_table
        return f"FOREIGN KEY ({cols}) REFERENCES {target}"
    if cons.type == ConstraintType.CHECK and cons.search_condition:
        condition = cons.search_condition.strip()
        # Oracle stores every NOT NULL as a check constraint; it is already on the column line.
        if re.fullmatch(r'"?\w+"?\s+IS\s+NOT\s+NULL', condition, flags=re.IGNORECASE):
            return ""
        return f"CHECK ({condition})"
    return ""


def _index_backs_a_constraint(index_name: str, table: TableInfo) -> bool:
    """True when an index shares its name with a PK/unique constraint (Oracle's usual pairing)."""
    return any(
        c.name == index_name
        for c in table.constraints
        if c.type in {ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE}
    )


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
