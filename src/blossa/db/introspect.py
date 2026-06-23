# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Read the Oracle data dictionary into Blossa's pydantic model. No LLM, no DML."""

from __future__ import annotations

from typing import Any

from ..models import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    IndexInfo,
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


def introspect_schema(db: QueryExecutor, owner: str) -> SchemaInfo:
    """Read all tables/columns/constraints/indexes for `owner` into a SchemaInfo."""
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


def introspect_schemas(db: QueryExecutor, owners: list[str]) -> SchemaInfo:
    """Introspect several owners and merge them into one SchemaInfo (tables tagged with owner)."""
    if len(owners) == 1:
        return introspect_schema(db, owners[0])
    merged = SchemaInfo(name="+".join(owners))
    for owner in owners:
        merged.tables.extend(introspect_schema(db, owner).tables)
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


# --------------------------------------------------------------------- helpers


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
