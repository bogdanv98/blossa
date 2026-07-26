# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Read the Oracle data dictionary into Blossa's pydantic model. No LLM, no DML."""

from __future__ import annotations

from typing import Any

from ..models import (
    CatalogObject,
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    IndexInfo,
    ProgramKind,
    ProgramUnit,
    SchedulerChain,
    SchedulerJob,
    SchedulerRule,
    SchedulerStep,
    SchemaInfo,
    TableInfo,
)
from .connection import QueryExecutor

# Only real, user tables (skip dropped/recycle-bin objects and IOT overflow segments).
_TABLES_SQL = """
    SELECT t.TABLE_NAME, t.NUM_ROWS
      FROM ALL_TABLES t
     WHERE t.OWNER = :owner
       AND t.TABLE_NAME NOT LIKE 'BIN$%'
       AND t.DROPPED = 'NO'
     ORDER BY t.TABLE_NAME
"""

_TABLE_COMMENTS_SQL = """
    SELECT TABLE_NAME, COMMENTS
      FROM ALL_TAB_COMMENTS
     WHERE OWNER = :owner AND COMMENTS IS NOT NULL
"""

_COLUMNS_SQL = """
    SELECT TABLE_NAME, COLUMN_NAME, COLUMN_ID, DATA_TYPE,
           DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, DATA_DEFAULT
      FROM ALL_TAB_COLUMNS
     WHERE OWNER = :owner
     ORDER BY TABLE_NAME, COLUMN_ID
"""

_COLUMN_COMMENTS_SQL = """
    SELECT TABLE_NAME, COLUMN_NAME, COMMENTS
      FROM ALL_COL_COMMENTS
     WHERE OWNER = :owner AND COMMENTS IS NOT NULL
"""

# Constraints + the columns that participate in them, in positional order.
_CONSTRAINTS_SQL = """
    SELECT c.CONSTRAINT_NAME, c.TABLE_NAME, c.CONSTRAINT_TYPE, c.STATUS,
           c.SEARCH_CONDITION, c.R_OWNER, c.R_CONSTRAINT_NAME
      FROM ALL_CONSTRAINTS c
     WHERE c.OWNER = :owner
       AND c.CONSTRAINT_TYPE IN ('P', 'R', 'U', 'C')
"""

_CONS_COLUMNS_SQL = """
    SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, POSITION
      FROM ALL_CONS_COLUMNS
     WHERE OWNER = :owner
     ORDER BY CONSTRAINT_NAME, POSITION
"""

_INDEXES_SQL = """
    SELECT INDEX_NAME, TABLE_NAME, UNIQUENESS
      FROM ALL_INDEXES
     WHERE TABLE_OWNER = :owner
       AND INDEX_NAME NOT LIKE 'BIN$%'
"""

_IND_COLUMNS_SQL = """
    SELECT INDEX_NAME, TABLE_NAME, COLUMN_NAME, COLUMN_POSITION
      FROM ALL_IND_COLUMNS
     WHERE INDEX_OWNER = :owner
     ORDER BY INDEX_NAME, COLUMN_POSITION
"""

# Program-unit source views. ALL_SOURCE is filtered by EXECUTE privilege, so a least-privilege
# reader cannot see another schema's PL/SQL through it; with the full profile (SELECT_CATALOG_ROLE)
# the DBA_* views show every unit's source. The column lists are identical across ALL_/DBA_, so we
# just swap the view name based on the catalog scope. {view} is an internal constant, never input.

# Stored PL/SQL: standalone procedures/functions and packages (spec + body). Ordered so the
# lines of each unit arrive in order; a package's spec ('PACKAGE') sorts before its body.
_SOURCE_SQL = """
    SELECT NAME, TYPE, LINE, TEXT
      FROM {view}
     WHERE OWNER = :owner
       AND TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
     ORDER BY NAME, TYPE, LINE
"""

_TRIGGERS_SQL = """
    SELECT TRIGGER_NAME, DESCRIPTION, TRIGGERING_EVENT, TABLE_NAME, TRIGGER_BODY
      FROM {view}
     WHERE OWNER = :owner
     ORDER BY TRIGGER_NAME
"""

_VIEWS_SQL = """
    SELECT VIEW_NAME, TEXT
      FROM {view}
     WHERE OWNER = :owner
     ORDER BY VIEW_NAME
"""

# The full object inventory for the browser. Bodies fold into their spec (PACKAGE BODY/TYPE BODY),
# partitions and LOB segments are storage detail, and GENERATED='N' drops the system-named indexes
# Oracle creates for constraints (SYS_C00...) — a user browsing objects doesn't want either.
_OBJECT_TYPES = (
    "TABLE", "VIEW", "PACKAGE", "PROCEDURE", "FUNCTION", "TRIGGER",
    "SEQUENCE", "SYNONYM", "MATERIALIZED VIEW", "TYPE", "INDEX",
    # Scheduler objects: a scheduled batch is part of what a schema *is*, and leaving these out
    # made a whole nightly process invisible. RULE / RULE SET / EVALUATION CONTEXT are omitted on
    # purpose — Oracle creates them as a chain's internal plumbing, and the rules are surfaced far
    # more usefully as the chain's edges (see `scheduler_chains`) than as anonymous catalog rows.
    "JOB", "PROGRAM", "CHAIN", "SCHEDULE", "JOB CLASS",
)

_OBJECTS_SQL = """
    SELECT OBJECT_NAME, OBJECT_TYPE, STATUS
      FROM {view}
     WHERE OWNER = :owner
       AND OBJECT_TYPE IN ({types})
       AND OBJECT_NAME NOT LIKE 'BIN$%'
       AND GENERATED = 'N'
     ORDER BY OBJECT_TYPE, OBJECT_NAME
"""

# --- DBMS_SCHEDULER --------------------------------------------------------------------------
# A chain is the only place the *order* of a batch is written down: the steps say which program
# each node runs, and the rules are the edges between them (fan-out, AND joins, error branch).
# Reading the packages alone shows the procedures but never the graph that drives them.
#
# Column lists here are the real ones from the 21c dictionary — verified, not assumed. ALL_ and
# DBA_ variants carry identical columns, so the view name swaps with the catalog scope.

_SCHED_CHAINS_SQL = """
    SELECT CHAIN_NAME, ENABLED, COMMENTS
      FROM {view}
     WHERE OWNER = :owner
     ORDER BY CHAIN_NAME
"""

_SCHED_STEPS_SQL = """
    SELECT CHAIN_NAME, STEP_NAME, PROGRAM_OWNER, PROGRAM_NAME, STEP_TYPE
      FROM {view}
     WHERE OWNER = :owner
     ORDER BY CHAIN_NAME, STEP_NAME
"""

_SCHED_RULES_SQL = """
    SELECT CHAIN_NAME, RULE_NAME, CONDITION, ACTION, COMMENTS
      FROM {view}
     WHERE OWNER = :owner
     ORDER BY CHAIN_NAME, RULE_NAME
"""

_SCHED_PROGRAMS_SQL = """
    SELECT PROGRAM_NAME, PROGRAM_ACTION
      FROM {view}
     WHERE OWNER = :owner
"""

# JOB_SUBNAME is set on the per-step job rows the scheduler spawns while a chain runs; those are
# transient execution detail, not objects the schema declares.
_SCHED_JOBS_SQL = """
    SELECT JOB_NAME, JOB_TYPE, JOB_ACTION, PROGRAM_NAME, REPEAT_INTERVAL,
           ENABLED, STATE, RESTARTABLE, LAST_START_DATE, NEXT_RUN_DATE, COMMENTS
      FROM {view}
     WHERE OWNER = :owner
       AND JOB_SUBNAME IS NULL
     ORDER BY JOB_NAME
"""

# ALL_SOURCE.TYPE -> the program kind we expose (spec and body both fold into one PACKAGE unit).
_KIND_BY_SOURCE_TYPE = {
    "PROCEDURE": ProgramKind.PROCEDURE,
    "FUNCTION": ProgramKind.FUNCTION,
    "PACKAGE": ProgramKind.PACKAGE,
    "PACKAGE BODY": ProgramKind.PACKAGE,
}


def introspect_schema(db: QueryExecutor, owner: str, use_dba: bool = False) -> SchemaInfo:
    """Read all tables/columns/constraints/indexes for `owner` into a SchemaInfo.

    `use_dba` selects the DBA_* program-source views (needs the full catalog profile) so a reader
    can capture other schemas' PL/SQL; otherwise the EXECUTE-filtered ALL_* views are used.
    """
    binds = {"owner": owner}

    tables = {r["TABLE_NAME"]: r for r in db.query(_TABLES_SQL, binds)}
    table_comments = {r["TABLE_NAME"]: r["COMMENTS"] for r in db.query(_TABLE_COMMENTS_SQL, binds)}
    col_comments = {
        (r["TABLE_NAME"], r["COLUMN_NAME"]): r["COMMENTS"]
        for r in db.query(_COLUMN_COMMENTS_SQL, binds)
    }

    columns_by_table = _group_columns(db.query(_COLUMNS_SQL, binds), col_comments)
    constraints_by_table = _build_constraints(db, binds)
    indexes_by_table = _build_indexes(db, binds)

    schema = SchemaInfo(name=owner)
    for table_name, trow in tables.items():
        schema.tables.append(
            TableInfo(
                name=table_name,
                owner=owner,
                comment=table_comments.get(table_name),
                num_rows=_as_int(trow.get("NUM_ROWS")),
                columns=columns_by_table.get(table_name, []),
                constraints=constraints_by_table.get(table_name, []),
                indexes=indexes_by_table.get(table_name, []),
            )
        )
    schema.program_units = _build_program_units(db, owner, binds, use_dba)
    schema.objects = _build_catalog_objects(db, owner, binds, use_dba)
    schema.scheduler_chains = _build_scheduler_chains(db, owner, binds, use_dba)
    schema.scheduler_jobs = _build_scheduler_jobs(db, owner, binds, use_dba)
    return schema


# Oracle-maintained schemas we never want to scan when the user asks for "all non-system".
_SYSTEM_SCHEMAS = (
    "SYS", "SYSTEM", "XDB", "MDSYS", "CTXSYS", "DBSNMP", "OUTLN", "GSMADMIN_INTERNAL",
    "LBACSYS", "DVSYS", "DVF", "AUDSYS", "APPQOSSYS", "OJVMSYS", "ORDSYS", "ORDDATA",
    "ORDPLUGINS", "SI_INFORMTN_SCHEMA", "WMSYS", "OLAPSYS", "REMOTE_SCHEDULER_AGENT",
    "ANONYMOUS", "GGSYS", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "SYS$UMF", "PDBADMIN",
    "FLOWS_FILES", "APEX_PUBLIC_USER", "DIP", "ORACLE_OCM", "XS$NULL",
)


def list_non_system_schemas(db: QueryExecutor) -> list[str]:
    """Every schema that actually owns a table and isn't an Oracle-maintained one.

    Oracle 12.2+ flags its own schemas with ALL_USERS.ORACLE_MAINTAINED='Y', which is the
    authoritative source — it catches internal schemas (e.g. DBSFWUSER) that a hand-kept
    blocklist inevitably misses. On older releases that column doesn't exist, so we fall back
    to the fixed `_SYSTEM_SCHEMAS` blocklist.
    """
    maintained_sql = """
        SELECT u.USERNAME AS OWNER
          FROM ALL_USERS u
         WHERE u.ORACLE_MAINTAINED = 'N'
           AND EXISTS (SELECT 1 FROM ALL_TABLES t WHERE t.OWNER = u.USERNAME)
         ORDER BY u.USERNAME
    """
    try:
        return [r["OWNER"] for r in db.query(maintained_sql)]
    except Exception:  # noqa: BLE001 - pre-12.2 has no ORACLE_MAINTAINED; use the blocklist instead
        placeholders = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
        sql = f"""
            SELECT DISTINCT OWNER FROM ALL_TABLES
             WHERE OWNER NOT IN ({placeholders})
               AND OWNER NOT LIKE 'APEX_%'
               AND OWNER NOT LIKE 'FLOWS_%'
             ORDER BY OWNER
        """  # noqa: S608 - the IN list is a fixed constant, not user input
        return [r["OWNER"] for r in db.query(sql)]


def introspect_schemas(db: QueryExecutor, owners: list[str], use_dba: bool = False) -> SchemaInfo:
    """Introspect several owners and merge them into one SchemaInfo (tables tagged with owner)."""
    if len(owners) == 1:
        return introspect_schema(db, owners[0], use_dba)
    merged = SchemaInfo(name="+".join(owners))
    for owner in owners:
        one = introspect_schema(db, owner, use_dba)
        merged.tables.extend(one.tables)
        merged.program_units.extend(one.program_units)
        merged.objects.extend(one.objects)
    return merged


def _group_columns(
    rows: list[dict[str, Any]],
    col_comments: dict[tuple[str, str], str],
) -> dict[str, list[ColumnInfo]]:
    out: dict[str, list[ColumnInfo]] = {}
    for r in rows:
        tname = r["TABLE_NAME"]
        out.setdefault(tname, []).append(
            ColumnInfo(
                name=r["COLUMN_NAME"],
                column_id=_as_int(r.get("COLUMN_ID")) or 0,
                data_type=r["DATA_TYPE"],
                data_length=_as_int(r.get("DATA_LENGTH")),
                data_precision=_as_int(r.get("DATA_PRECISION")),
                data_scale=_as_int(r.get("DATA_SCALE")),
                nullable=(r.get("NULLABLE") == "Y"),
                data_default=_clean_default(r.get("DATA_DEFAULT")),
                comment=col_comments.get((tname, r["COLUMN_NAME"])),
            )
        )
    return out


def _build_constraints(db: QueryExecutor, binds: dict[str, Any]) -> dict[str, list[ConstraintInfo]]:
    cons_rows = db.query(_CONSTRAINTS_SQL, binds)
    cons_cols = db.query(_CONS_COLUMNS_SQL, binds)

    # constraint_name -> ordered list of column names
    cols_by_cons: dict[str, list[str]] = {}
    for r in cons_cols:
        cols_by_cons.setdefault(r["CONSTRAINT_NAME"], []).append(r["COLUMN_NAME"])

    # Index by constraint name so we can resolve FK -> referenced (table, columns).
    by_name = {r["CONSTRAINT_NAME"]: r for r in cons_rows}

    # A FK may reference a key in ANOTHER schema; those referenced constraints aren't in this
    # owner's rows, so resolve them with a targeted lookup keyed by (R_OWNER, R_CONSTRAINT_NAME).
    cross = {
        (r.get("R_OWNER"), r.get("R_CONSTRAINT_NAME"))
        for r in cons_rows
        if ConstraintType(r["CONSTRAINT_TYPE"]) == ConstraintType.FOREIGN_KEY
        and r.get("R_CONSTRAINT_NAME") not in by_name
        and r.get("R_OWNER")
        and r.get("R_CONSTRAINT_NAME")
    }
    cross_ref = _resolve_referenced(db, cross)

    out: dict[str, list[ConstraintInfo]] = {}
    for r in cons_rows:
        cname = r["CONSTRAINT_NAME"]
        ctype = ConstraintType(r["CONSTRAINT_TYPE"])
        referenced_table: str | None = None
        referenced_columns: list[str] = []
        if ctype == ConstraintType.FOREIGN_KEY:
            ref = by_name.get(r.get("R_CONSTRAINT_NAME"))
            if ref is not None:  # same-schema reference
                referenced_table = ref["TABLE_NAME"]
                referenced_columns = cols_by_cons.get(ref["CONSTRAINT_NAME"], [])
            else:  # cross-schema reference, resolved separately
                resolved = cross_ref.get((r.get("R_OWNER"), r.get("R_CONSTRAINT_NAME")))
                if resolved is not None:
                    referenced_table, referenced_columns = resolved

        out.setdefault(r["TABLE_NAME"], []).append(
            ConstraintInfo(
                name=cname,
                type=ctype,
                columns=cols_by_cons.get(cname, []),
                referenced_table=referenced_table,
                referenced_columns=referenced_columns,
                search_condition=_to_text(r.get("SEARCH_CONDITION")),
                status=r.get("STATUS"),
            )
        )
    return out


def _resolve_referenced(
    db: QueryExecutor, refs: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str, list[str]]]:
    """Resolve (owner, constraint) -> (table, columns) for keys referenced from another schema."""
    out: dict[tuple[str, str], tuple[str, list[str]]] = {}
    by_owner: dict[str, set[str]] = {}
    for r_owner, r_cons in refs:
        by_owner.setdefault(r_owner, set()).add(r_cons)
    for r_owner, names in by_owner.items():
        in_list = ", ".join(f"'{n}'" for n in sorted(names))
        # Identifiers come from the data dictionary (R_OWNER / R_CONSTRAINT_NAME), not user input.
        crows = db.query(  # noqa: S608
            f"SELECT CONSTRAINT_NAME, TABLE_NAME FROM ALL_CONSTRAINTS "
            f"WHERE OWNER = '{r_owner}' AND CONSTRAINT_NAME IN ({in_list})"
        )
        ccols = db.query(  # noqa: S608
            f"SELECT CONSTRAINT_NAME, COLUMN_NAME, POSITION FROM ALL_CONS_COLUMNS "
            f"WHERE OWNER = '{r_owner}' AND CONSTRAINT_NAME IN ({in_list}) "
            f"ORDER BY CONSTRAINT_NAME, POSITION"
        )
        cols_by: dict[str, list[str]] = {}
        for r in ccols:
            cols_by.setdefault(r["CONSTRAINT_NAME"], []).append(r["COLUMN_NAME"])
        for r in crows:
            cname = r["CONSTRAINT_NAME"]
            out[(r_owner, cname)] = (r["TABLE_NAME"], cols_by.get(cname, []))
    return out


def _build_indexes(db: QueryExecutor, binds: dict[str, Any]) -> dict[str, list[IndexInfo]]:
    idx_rows = db.query(_INDEXES_SQL, binds)
    idx_cols = db.query(_IND_COLUMNS_SQL, binds)

    cols_by_idx: dict[str, list[str]] = {}
    for r in idx_cols:
        cols_by_idx.setdefault(r["INDEX_NAME"], []).append(r["COLUMN_NAME"])

    out: dict[str, list[IndexInfo]] = {}
    for r in idx_rows:
        out.setdefault(r["TABLE_NAME"], []).append(
            IndexInfo(
                name=r["INDEX_NAME"],
                unique=(r.get("UNIQUENESS") == "UNIQUE"),
                columns=cols_by_idx.get(r["INDEX_NAME"], []),
            )
        )
    return out


def _build_program_units(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool = False
) -> list[ProgramUnit]:
    """Capture stored program units (procedures/functions/packages/triggers) and views + source.

    Source is PL/SQL / a view's defining SELECT — DDL, not row data. Each dictionary view is read
    independently and a read failure (e.g. no privilege on the source view) degrades to skipping
    that kind rather than aborting the whole scan. `use_dba` picks DBA_* over ALL_* (see above).
    """
    return [
        *_plsql_units(db, owner, binds, use_dba),
        *_trigger_units(db, owner, binds, use_dba),
        *_view_units(db, owner, binds, use_dba),
    ]


def _build_catalog_objects(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool = False
) -> list[CatalogObject]:
    """List every browsable object the owner has (names + status only, no source).

    Cheap and privilege-tolerant: a failure here just means the object browser falls back to
    tables + program units, so it must never abort a scan.
    """
    view = "DBA_OBJECTS" if use_dba else "ALL_OBJECTS"
    types = ", ".join(f"'{t}'" for t in _OBJECT_TYPES)
    sql = _OBJECTS_SQL.format(view=view, types=types)  # noqa: S608 - both are fixed constants
    try:
        rows = db.query(sql, binds)
    except Exception:  # noqa: BLE001 - no access to the object catalog: browse what we have
        return []
    return [
        CatalogObject(
            name=r["OBJECT_NAME"],
            owner=owner,
            type=(r.get("OBJECT_TYPE") or "").strip(),
            status=(r.get("STATUS") or "").strip(),
        )
        for r in rows
    ]


def _sched_rows(
    db: QueryExecutor, sql: str, view: str, binds: dict[str, Any]
) -> list[dict[str, Any]]:
    """Run one scheduler-dictionary query, tolerating an account that cannot see the view.

    The scheduler views are readable by any account for its own objects, but a locked-down or
    pre-10g target may still refuse. A refusal means "no scheduler section", never a failed scan.
    """
    try:
        return db.query(sql.format(view=view), binds)  # noqa: S608 - view is a constant
    except Exception:  # noqa: BLE001 - no access to the scheduler catalog: skip, keep scanning
        return []


def _build_scheduler_chains(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool
) -> list[SchedulerChain]:
    """Read every job chain the owner declares, with its steps and its rules."""
    prefix = "DBA" if use_dba else "ALL"
    chains = _sched_rows(db, _SCHED_CHAINS_SQL, f"{prefix}_SCHEDULER_CHAINS", binds)
    if not chains:
        return []

    steps = _sched_rows(db, _SCHED_STEPS_SQL, f"{prefix}_SCHEDULER_CHAIN_STEPS", binds)
    rules = _sched_rows(db, _SCHED_RULES_SQL, f"{prefix}_SCHEDULER_CHAIN_RULES", binds)
    programs = _sched_rows(db, _SCHED_PROGRAMS_SQL, f"{prefix}_SCHEDULER_PROGRAMS", binds)

    # A step points at a program; the program points at the procedure. Resolving it here is what
    # lets a reader jump from "step S04" to the packaged routine the Logic tab already explains.
    action_by_program = {
        r["PROGRAM_NAME"]: (_to_text(r.get("PROGRAM_ACTION")) or "").strip() for r in programs
    }

    steps_by_chain: dict[str, list[SchedulerStep]] = {}
    for r in steps:
        program_owner = r.get("PROGRAM_OWNER")
        program_name = r.get("PROGRAM_NAME")
        steps_by_chain.setdefault(r["CHAIN_NAME"], []).append(
            SchedulerStep(
                name=r["STEP_NAME"],
                program_owner=program_owner,
                program_name=program_name,
                step_type=(r.get("STEP_TYPE") or "").strip(),
                # Only own-schema programs were read, so a cross-schema step keeps an empty action
                # rather than borrowing an unrelated program of the same name.
                action=(
                    action_by_program.get(program_name, "")
                    if program_owner == owner
                    else ""
                ),
            )
        )

    rules_by_chain: dict[str, list[SchedulerRule]] = {}
    for r in rules:
        rules_by_chain.setdefault(r["CHAIN_NAME"], []).append(
            SchedulerRule(
                name=r["RULE_NAME"],
                condition=(_to_text(r.get("CONDITION")) or "").strip(),
                action=(_to_text(r.get("ACTION")) or "").strip(),
                comment=_to_text(r.get("COMMENTS")),
            )
        )

    return [
        SchedulerChain(
            name=c["CHAIN_NAME"],
            owner=owner,
            enabled=(c.get("ENABLED") or "").upper() == "TRUE",
            comment=_to_text(c.get("COMMENTS")),
            steps=steps_by_chain.get(c["CHAIN_NAME"], []),
            rules=rules_by_chain.get(c["CHAIN_NAME"], []),
        )
        for c in chains
    ]


def _build_scheduler_jobs(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool
) -> list[SchedulerJob]:
    """Read the owner's scheduled jobs — what runs, on what cadence, and whether it is armed."""
    prefix = "DBA" if use_dba else "ALL"
    rows = _sched_rows(db, _SCHED_JOBS_SQL, f"{prefix}_SCHEDULER_JOBS", binds)
    return [
        SchedulerJob(
            name=r["JOB_NAME"],
            owner=owner,
            job_type=(r.get("JOB_TYPE") or "").strip(),
            job_action=(_to_text(r.get("JOB_ACTION")) or "").strip(),
            program_name=r.get("PROGRAM_NAME"),
            repeat_interval=(_to_text(r.get("REPEAT_INTERVAL")) or "").strip(),
            enabled=(r.get("ENABLED") or "").upper() == "TRUE",
            state=(r.get("STATE") or "").strip(),
            restartable=(r.get("RESTARTABLE") or "").upper() == "TRUE",
            last_start=_as_stamp(r.get("LAST_START_DATE")),
            next_run=_as_stamp(r.get("NEXT_RUN_DATE")),
            comment=_to_text(r.get("COMMENTS")),
        )
        for r in rows
    ]


def _plsql_units(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool
) -> list[ProgramUnit]:
    view = "DBA_SOURCE" if use_dba else "ALL_SOURCE"
    try:
        rows = db.query(_SOURCE_SQL.format(view=view), binds)  # noqa: S608 - view is a constant
    except Exception:  # noqa: BLE001 - no access to the source view: skip PL/SQL, keep scanning
        return []
    # Accumulate lines per unit name, preserving the (TYPE, LINE) order from the query.
    by_name: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r["NAME"]
        kind = _KIND_BY_SOURCE_TYPE.get(r["TYPE"], ProgramKind.PROCEDURE)
        entry = by_name.setdefault(name, {"kind": kind, "parts": []})
        entry["parts"].append(_to_text(r.get("TEXT")) or "")
    return [
        ProgramUnit(name=name, owner=owner, kind=e["kind"], source="".join(e["parts"]).strip())
        for name, e in by_name.items()
    ]


def _trigger_units(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool
) -> list[ProgramUnit]:
    view = "DBA_TRIGGERS" if use_dba else "ALL_TRIGGERS"
    try:
        rows = db.query(_TRIGGERS_SQL.format(view=view), binds)  # noqa: S608 - view is a constant
    except Exception:  # noqa: BLE001 - no access to the triggers view: skip triggers, keep scanning
        return []
    units = []
    for r in rows:
        header = (_to_text(r.get("DESCRIPTION")) or "").strip()
        event = f"-- {r.get('TRIGGERING_EVENT')} ON {r.get('TABLE_NAME')}"
        body = (_to_text(r.get("TRIGGER_BODY")) or "").strip()
        source = "\n".join(part for part in (event, header, body) if part)
        units.append(
            ProgramUnit(
                name=r["TRIGGER_NAME"], owner=owner, kind=ProgramKind.TRIGGER, source=source
            )
        )
    return units


def _view_units(
    db: QueryExecutor, owner: str, binds: dict[str, Any], use_dba: bool
) -> list[ProgramUnit]:
    view = "DBA_VIEWS" if use_dba else "ALL_VIEWS"
    try:
        rows = db.query(_VIEWS_SQL.format(view=view), binds)  # noqa: S608 - view is a constant
    except Exception:  # noqa: BLE001 - no access to the views view: skip views, keep scanning
        return []
    return [
        ProgramUnit(
            name=r["VIEW_NAME"],
            owner=owner,
            kind=ProgramKind.VIEW,
            source=(_to_text(r.get("TEXT")) or "").strip(),
        )
        for r in rows
    ]


# --------------------------------------------------------------------- helpers


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_stamp(value: Any) -> str:
    """A scheduler timestamp as a plain 'YYYY-MM-DD HH:MM' string (they arrive tz-aware)."""
    if value is None:
        return ""
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except AttributeError:
        return str(value)


def _clean_default(value: Any) -> str | None:
    text = _to_text(value)
    return text.strip() if text else None


def _to_text(value: Any) -> str | None:
    """LONG / CLOB-ish columns (SEARCH_CONDITION, DATA_DEFAULT) may arrive as LOBs."""
    if value is None:
        return None
    if hasattr(value, "read"):
        return value.read()
    return str(value)
