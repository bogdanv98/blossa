# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Deterministic checks over an introspected schema (and, when available, its data).

Produces:
  * relationships  — declared FKs plus inferred/undeclared candidate FKs
  * findings       — orphan rows, type & naming inconsistencies, missing comments

Structure-only checks always run. The two data-dependent checks (candidate-FK value overlap
and orphan counting) only run when a live `QueryExecutor` is provided; without one, candidate
FKs are still inferred by name but flagged LOW confidence ("name match only").
"""

from __future__ import annotations

from ..db.connection import QueryExecutor
from ..models import (
    ConfidenceLevel,
    ConstraintType,
    Finding,
    FindingKind,
    Relationship,
    SchemaInfo,
    Severity,
)

# Suffix pairs that usually mean the same thing — using both is a naming inconsistency.
_SYNONYM_SUFFIXES = [("_DT", "_DATE"), ("_NO", "_NUM"), ("_CD", "_CODE"), ("_ID", "_KEY")]


def run_checks(
    schema: SchemaInfo,
    db: QueryExecutor | None = None,
    owner: str | None = None,
    overlap_threshold: float = 0.85,
) -> tuple[list[Relationship], list[Finding]]:
    relationships: list[Relationship] = []
    findings: list[Finding] = []

    relationships.extend(_declared_relationships(schema))

    candidates, orphan_findings = _candidate_foreign_keys(
        schema, relationships, db, owner, overlap_threshold
    )
    relationships.extend(candidates)
    findings.extend(orphan_findings)

    findings.extend(_type_inconsistencies(schema))
    findings.extend(_naming_inconsistencies(schema))
    findings.extend(_missing_comments(schema))
    return relationships, findings


# ----------------------------------------------------------------- relationships


def _declared_relationships(schema: SchemaInfo) -> list[Relationship]:
    rels: list[Relationship] = []
    for table in schema.tables:
        for fk in table.foreign_keys:
            if not fk.referenced_table:
                continue
            rels.append(
                Relationship(
                    from_table=table.name,
                    from_columns=fk.columns,
                    to_table=fk.referenced_table,
                    to_columns=fk.referenced_columns,
                    declared=True,
                    confidence=ConfidenceLevel.HIGH,
                    evidence=[f"Declared foreign key constraint {fk.name}."],
                )
            )
    return rels


def _single_col_keys(schema: SchemaInfo) -> dict[str, tuple[str, str]]:
    """Map PK/unique single-column key column-name -> (table, column) for parent matching."""
    keys: dict[str, tuple[str, str]] = {}
    for table in schema.tables:
        for c in table.constraints:
            is_key = c.type in (ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE)
            if is_key and len(c.columns) == 1:
                keys.setdefault(c.columns[0], (table.name, c.columns[0]))
    return keys


def _candidate_foreign_keys(
    schema: SchemaInfo,
    declared: list[Relationship],
    db: QueryExecutor | None,
    owner: str | None,
    overlap_threshold: float,
) -> tuple[list[Relationship], list[Finding]]:
    parents = _single_col_keys(schema)
    declared_pairs = {(r.from_table, tuple(r.from_columns)) for r in declared}

    candidates: list[Relationship] = []
    findings: list[Finding] = []

    for table in schema.tables:
        for col in table.columns:
            parent = parents.get(col.name)
            if parent is None:
                continue
            parent_table, parent_col = parent
            if parent_table == table.name:
                continue  # this column IS the key, not a reference to another table
            if (table.name, (col.name,)) in declared_pairs:
                continue  # already a declared FK

            evidence = [f"Column name matches {parent_table}.{parent_col} (a key column)."]
            parent_info = schema.table(parent_table)
            child_type = col.type_signature
            parent_type = (
                parent_info.column(parent_col).type_signature
                if parent_info and parent_info.column(parent_col)
                else None
            )
            if parent_type and parent_type.split("(")[0] != child_type.split("(")[0]:
                evidence.append(f"Type differs: {child_type} vs {parent_type} (compared as text).")

            confidence = ConfidenceLevel.LOW
            if db is not None and owner is not None:
                overlap, orphan_rows = _value_overlap(
                    db, owner, table.name, col.name, parent_table, parent_col
                )
                if overlap is None or overlap < overlap_threshold:
                    continue  # data does not support a relationship
                evidence.append(f"{overlap:.0%} of distinct values exist in the parent key.")
                confidence = (
                    ConfidenceLevel.HIGH if overlap >= 0.99 else ConfidenceLevel.MEDIUM
                )
                if orphan_rows > 0:
                    findings.append(
                        Finding(
                            kind=FindingKind.ORPHAN_ROWS,
                            severity=Severity.WARNING,
                            table=table.name,
                            columns=[col.name],
                            message=(
                                f"{orphan_rows} row(s) in {table.name}.{col.name} have no "
                                f"matching {parent_table}.{parent_col} (undeclared FK)."
                            ),
                            details={"orphan_rows": orphan_rows, "overlap": round(overlap, 4)},
                        )
                    )

            candidates.append(
                Relationship(
                    from_table=table.name,
                    from_columns=[col.name],
                    to_table=parent_table,
                    to_columns=[parent_col],
                    declared=False,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
            findings.append(
                Finding(
                    kind=FindingKind.UNDECLARED_FK_CANDIDATE,
                    severity=Severity.NOTICE,
                    table=table.name,
                    columns=[col.name],
                    message=(
                        f"Likely undeclared foreign key: {table.name}.{col.name} -> "
                        f"{parent_table}.{parent_col} ({confidence.value} confidence)."
                    ),
                )
            )
    return candidates, findings


def _value_overlap(
    db: QueryExecutor,
    owner: str,
    child_table: str,
    child_col: str,
    parent_table: str,
    parent_col: str,
) -> tuple[float | None, int]:
    """Fraction of distinct child values present in the parent key, and orphan row count.

    Values are compared as text (TO_CHAR) so a VARCHAR child can still match a NUMBER parent.
    """
    child = f'"{owner}"."{child_table}"'
    parent = f'"{owner}"."{parent_table}"'
    cc, pc = f'"{child_col}"', f'"{parent_col}"'
    sql = f"""
        SELECT
            (SELECT COUNT(DISTINCT TO_CHAR({cc}))
               FROM {child} WHERE {cc} IS NOT NULL) AS DISTINCT_TOTAL,
            (SELECT COUNT(*) FROM (
                 SELECT DISTINCT TO_CHAR({cc}) AS V FROM {child} WHERE {cc} IS NOT NULL
             ) d
             WHERE EXISTS (
                 SELECT 1 FROM {parent} p WHERE TO_CHAR(p.{pc}) = d.V
             )) AS MATCHED,
            (SELECT COUNT(*) FROM {child} c
              WHERE c.{cc} IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM {parent} p WHERE TO_CHAR(p.{pc}) = TO_CHAR(c.{cc})
                )) AS ORPHAN_ROWS
        FROM dual
    """  # noqa: S608 - all identifiers come from the Oracle data dictionary, not user input
    try:
        row = db.query(sql)[0]
    except Exception:  # noqa: BLE001 - profiling/overlap is best-effort, never fatal
        return None, 0
    total = int(row.get("DISTINCT_TOTAL", 0) or 0)
    matched = int(row.get("MATCHED", 0) or 0)
    orphan_rows = int(row.get("ORPHAN_ROWS", 0) or 0)
    if total == 0:
        return None, 0
    return matched / total, orphan_rows


# ------------------------------------------------------------------- findings


def _type_inconsistencies(schema: SchemaInfo) -> list[Finding]:
    # column name -> {base_type -> [tables]}
    by_name: dict[str, dict[str, list[str]]] = {}
    for table in schema.tables:
        for col in table.columns:
            base = col.data_type.upper()
            by_name.setdefault(col.name, {}).setdefault(base, []).append(table.name)

    findings: list[Finding] = []
    for col_name, types in by_name.items():
        if len(types) > 1:
            desc = "; ".join(f"{t} in {', '.join(sorted(tables))}" for t, tables in types.items())
            findings.append(
                Finding(
                    kind=FindingKind.TYPE_INCONSISTENCY,
                    severity=Severity.WARNING,
                    columns=[col_name],
                    message=f"Column '{col_name}' has inconsistent types across tables: {desc}.",
                )
            )
    return findings


def _naming_inconsistencies(schema: SchemaInfo) -> list[Finding]:
    all_columns = {col.name.upper() for table in schema.tables for col in table.columns}
    findings: list[Finding] = []
    for a_suffix, b_suffix in _SYNONYM_SUFFIXES:
        a_cols = sorted(c for c in all_columns if c.endswith(a_suffix))
        b_cols = sorted(c for c in all_columns if c.endswith(b_suffix))
        if a_cols and b_cols:
            findings.append(
                Finding(
                    kind=FindingKind.NAMING_INCONSISTENCY,
                    severity=Severity.NOTICE,
                    message=(
                        f"Mixed naming convention '{a_suffix}' vs '{b_suffix}': "
                        f"{', '.join(a_cols)} alongside {', '.join(b_cols)}."
                    ),
                )
            )
    return findings


def _missing_comments(schema: SchemaInfo) -> list[Finding]:
    findings: list[Finding] = []
    for table in schema.tables:
        if not (table.comment and table.comment.strip()):
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_TABLE_COMMENT,
                    severity=Severity.INFO,
                    table=table.name,
                    message=f"Table '{table.name}' has no comment.",
                )
            )
        uncommented = [c.name for c in table.columns if not (c.comment and c.comment.strip())]
        if uncommented:
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_COLUMN_COMMENT,
                    severity=Severity.INFO,
                    table=table.name,
                    columns=uncommented,
                    message=(
                        f"{len(uncommented)} of {len(table.columns)} columns in "
                        f"'{table.name}' have no comment."
                    ),
                    details={"uncommented": len(uncommented), "total": len(table.columns)},
                )
            )
    return findings
