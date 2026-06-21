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
    TableInfo,
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
                    from_owner=table.owner,
                    to_owner=table.owner,
                )
            )
    return rels


# A single-column key parent: (owner, table, column, base_type).
SingleKey = tuple[str, str, str, str]
# A composite key parent: (owner, table, (column, ...)).
CompositeKey = tuple[str, str, tuple[str, ...]]


def _table_owner(table: TableInfo, fallback: str | None) -> str:
    """A table's owner, falling back to the scan's primary owner for un-tagged fixtures."""
    return table.owner or (fallback or "")


def _single_col_keys(schema: SchemaInfo, fallback: str | None) -> dict[str, list[SingleKey]]:
    """Map key column-name -> every single-column PK/unique key with that name (across owners)."""
    keys: dict[str, list[SingleKey]] = {}
    for table in schema.tables:
        owner = _table_owner(table, fallback)
        for c in table.constraints:
            is_key = c.type in (ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE)
            if is_key and len(c.columns) == 1:
                kc = table.column(c.columns[0])
                base = _base_type(kc.type_signature) if kc else ""
                keys.setdefault(c.columns[0], []).append(
                    (owner, table.name, c.columns[0], base)
                )
    return keys


def _composite_keys(schema: SchemaInfo, fallback: str | None) -> list[CompositeKey]:
    """Every multi-column PK/unique key as (owner, table, (col, ...)), for composite-FK matching."""
    keys: list[CompositeKey] = []
    for table in schema.tables:
        owner = _table_owner(table, fallback)
        for c in table.constraints:
            is_key = c.type in (ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE)
            if is_key and len(c.columns) >= 2:
                keys.append((owner, table.name, tuple(c.columns)))
    return keys


def _base_type(type_signature: str) -> str:
    """The type name without precision/scale, e.g. NUMBER(10,0) -> NUMBER."""
    return type_signature.split("(")[0]


def _name_token(column_name: str) -> str:
    """The trailing underscore-delimited token, e.g. MANAGER_ID -> ID, EMPLOYEE_ID -> ID."""
    return column_name.rsplit("_", 1)[-1] if "_" in column_name else column_name


def _candidate_foreign_keys(
    schema: SchemaInfo,
    declared: list[Relationship],
    db: QueryExecutor | None,
    owner: str | None,
    overlap_threshold: float,
) -> tuple[list[Relationship], list[Finding]]:
    fb = owner  # fallback owner for fixtures whose tables aren't owner-tagged
    parents = _single_col_keys(schema, fb)          # col name -> [(owner, table, col, base), ...]
    key_list = [k for ks in parents.values() for k in ks]
    composite_parents = _composite_keys(schema, fb)
    declared_pairs = {
        (r.from_owner or fb or "", r.from_table, tuple(r.from_columns)) for r in declared
    }

    candidates: list[Relationship] = []
    findings: list[Finding] = []
    # (owner, table, column) tuples already paired off, so later passes never double-count them.
    matched_cols: set[tuple[str, str, str]] = set()

    def emit(
        child_owner: str, table_name: str, from_cols: list[str],
        parent_owner: str, parent_table: str, parent_cols: list[str],
        confidence: ConfidenceLevel, evidence: list[str], orphan_rows: int, overlap: float | None,
    ) -> None:
        for c in from_cols:
            matched_cols.add((child_owner, table_name, c))
        cross = bool(child_owner and parent_owner and child_owner != parent_owner)
        child_ref = f"{table_name}({', '.join(from_cols)})"
        pq = f"{parent_owner}.{parent_table}" if cross else parent_table
        parent_ref = f"{pq}({', '.join(parent_cols)})"
        candidates.append(
            Relationship(
                from_table=table_name,
                from_columns=from_cols,
                to_table=parent_table,
                to_columns=parent_cols,
                declared=False,
                confidence=confidence,
                evidence=evidence,
                from_owner=child_owner or None,
                to_owner=parent_owner or None,
            )
        )
        if orphan_rows > 0 and overlap is not None:
            findings.append(
                Finding(
                    kind=FindingKind.ORPHAN_ROWS,
                    severity=Severity.WARNING,
                    table=table_name,
                    columns=from_cols,
                    message=(
                        f"{orphan_rows} row(s) in {child_ref} have no "
                        f"matching {parent_ref} (undeclared FK)."
                    ),
                    details={"orphan_rows": orphan_rows, "overlap": round(overlap, 4)},
                )
            )
        findings.append(
            Finding(
                kind=FindingKind.UNDECLARED_FK_CANDIDATE,
                severity=Severity.NOTICE,
                table=table_name,
                columns=from_cols,
                message=(
                    f"Likely undeclared foreign key: {child_ref} -> "
                    f"{parent_ref} ({confidence.value} confidence)."
                ),
            )
        )

    def name_note(parent_owner: str, parent_table: str, parent_col: str, child_owner: str) -> str:
        cross = " (cross-schema)" if parent_owner and parent_owner != child_owner else ""
        return f"Column name matches {parent_table}.{parent_col} (a key column){cross}."

    # Pass 1 — exact name match: a column named exactly like a key column elsewhere. When more
    # than one schema owns such a key, the data overlap decides which one (and, with no DB to
    # decide, an ambiguous match is left alone).
    for table in schema.tables:
        co = _table_owner(table, fb)
        for col in table.columns:
            cands = [
                k for k in parents.get(col.name, [])
                if not (k[0] == co and k[1] == table.name)  # drop the column's own key
            ]
            if not cands:
                continue
            if (co, table.name, (col.name,)) in declared_pairs:
                continue
            child_base = _base_type(col.type_signature)

            if db is not None:
                best: tuple[float, int, str, str, str] | None = None
                for po, pt, pc, _pb in cands:
                    ov, orph = _value_overlap(db, co, table.name, col.name, po, pt, pc)
                    if ov is None or ov < overlap_threshold:
                        continue
                    if best is None or ov > best[0]:
                        best = (ov, orph, po, pt, pc)
                if best is None:
                    continue
                ov, orph, po, pt, pc = best
                pb = next((b for (o, t, c, b) in cands if o == po and t == pt and c == pc), "")
                evidence = [name_note(po, pt, pc, co)]
                if pb and pb != child_base:
                    evidence.append(f"Type differs from parent: {col.type_signature} vs {pb}.")
                evidence.append(f"{ov:.0%} of distinct values exist in the parent key.")
                conf = ConfidenceLevel.HIGH if ov >= 0.99 else ConfidenceLevel.MEDIUM
                emit(co, table.name, [col.name], po, pt, [pc], conf, evidence, orph, ov)
            else:
                if len(cands) != 1:
                    continue  # ambiguous without data to disambiguate
                po, pt, pc, pb = cands[0]
                evidence = [name_note(po, pt, pc, co)]
                if pb and pb != child_base:
                    evidence.append(f"Type differs from parent: {col.type_signature} vs {pb}.")
                emit(co, table.name, [col.name], po, pt, [pc],
                     ConfidenceLevel.LOW, evidence, 0, None)

    # Pass 2 — suffix match, confirmed by data. Catches role/self refs whose name does NOT
    # equal the key (MANAGER_ID -> EMPLOYEE_ID, or a cross-schema SALES_REP_ID -> HR.EMPLOYEES):
    # same trailing token ("_ID"), same base type, and a high value overlap. Data-gated to protect
    # precision, so it only runs against a live database — never offline.
    if db is not None:
        own_keys = {(po, pt, pc) for (po, pt, pc, _pb) in key_list}
        for table in schema.tables:
            co = _table_owner(table, fb)
            for col in table.columns:
                if (co, table.name, col.name) in matched_cols:
                    continue
                if (co, table.name, (col.name,)) in declared_pairs:
                    continue
                if (co, table.name, col.name) in own_keys:
                    continue  # this column is its own table's key, not a reference to another
                token = _name_token(col.name)
                child_base = _base_type(col.type_signature)
                compatible = [
                    (po, pt, pc) for (po, pt, pc, pb) in key_list
                    if _name_token(pc) == token and pb == child_base
                    and not (po == co and pt == table.name and pc == col.name)
                ]
                best2: tuple[float, int, str, str, str] | None = None
                for po, pt, pc in compatible:
                    ov, orph = _value_overlap(db, co, table.name, col.name, po, pt, pc)
                    if ov is None or ov < overlap_threshold:
                        continue
                    if best2 is None or ov > best2[0]:
                        best2 = (ov, orph, po, pt, pc)
                if best2 is None:
                    continue
                ov, orph, po, pt, pc = best2
                tags = ""
                if po == co and pt == table.name:
                    tags = " (self-reference)"
                elif po != co:
                    tags = " (cross-schema)"
                evidence = [
                    f"Name suffix '{token}' and type match {pt}.{pc}{tags}.",
                    f"{ov:.0%} of distinct values exist in the parent key.",
                ]
                conf = ConfidenceLevel.HIGH if ov >= 0.99 else ConfidenceLevel.MEDIUM
                emit(co, table.name, [col.name], po, pt, [pc], conf, evidence, orph, ov)

    # Pass 3 — composite keys: a table that carries every column of another table's multi-column
    # key (matched by name) likely references it. Confirmed by tuple overlap when a DB is present;
    # offline it is reported LOW, mirroring the single-column exact-name pass.
    for table in schema.tables:
        co = _table_owner(table, fb)
        col_names = {c.name for c in table.columns}
        for po, pt, key_cols in composite_parents:
            if po == co and pt == table.name:
                continue
            if not set(key_cols).issubset(col_names):
                continue  # child lacks one or more of the key's columns
            from_cols = list(key_cols)  # keep the parent key's column order
            if (co, table.name, tuple(from_cols)) in declared_pairs:
                continue

            cross = " (cross-schema)" if po != co else ""
            evidence = [
                f"Carries all columns of {pt}'s composite key ({', '.join(key_cols)}){cross}."
            ]
            confidence = ConfidenceLevel.LOW
            overlap: float | None = None
            orphan_rows = 0
            if db is not None:
                overlap, orphan_rows = _composite_value_overlap(
                    db, co, table.name, from_cols, po, pt, list(key_cols)
                )
                if overlap is None or overlap < overlap_threshold:
                    continue  # data does not support the relationship
                evidence.append(f"{overlap:.0%} of distinct value tuples exist in the parent key.")
                confidence = ConfidenceLevel.HIGH if overlap >= 0.99 else ConfidenceLevel.MEDIUM

            emit(co, table.name, from_cols, po, pt, list(key_cols),
                 confidence, evidence, orphan_rows, overlap)

    return candidates, findings


def _value_overlap(
    db: QueryExecutor,
    child_owner: str,
    child_table: str,
    child_col: str,
    parent_owner: str,
    parent_table: str,
    parent_col: str,
) -> tuple[float | None, int]:
    """Fraction of distinct child values present in the parent key, and orphan row count.

    Child and parent may live in different schemas (cross-schema FK), hence separate owners.
    Values are compared as text (TO_CHAR) so a VARCHAR child can still match a NUMBER parent.
    """
    child = f'"{child_owner}"."{child_table}"'
    parent = f'"{parent_owner}"."{parent_table}"'
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


def _composite_value_overlap(
    db: QueryExecutor,
    child_owner: str,
    child_table: str,
    child_cols: list[str],
    parent_owner: str,
    parent_table: str,
    parent_cols: list[str],
) -> tuple[float | None, int]:
    """Like `_value_overlap`, but for a multi-column key: the fraction of distinct child value
    *tuples* present in the parent key, plus the orphan row count. Columns compared as text."""
    child = f'"{child_owner}"."{child_table}"'
    parent = f'"{parent_owner}"."{parent_table}"'
    cc = [f'"{c}"' for c in child_cols]
    pc = [f'"{c}"' for c in parent_cols]
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in cc)
    select_cols = ", ".join(f"TO_CHAR({c}) AS C{i}" for i, c in enumerate(cc))
    join_d = " AND ".join(f"TO_CHAR(p.{pc[i]}) = d.C{i}" for i in range(len(pc)))
    join_c = " AND ".join(f"TO_CHAR(p.{pc[i]}) = TO_CHAR(c.{cc[i]})" for i in range(len(pc)))
    sql = f"""
        SELECT
            (SELECT COUNT(*) FROM (
                 SELECT DISTINCT {select_cols} FROM {child} WHERE {not_null}
             )) AS DISTINCT_TOTAL,
            (SELECT COUNT(*) FROM (
                 SELECT DISTINCT {select_cols} FROM {child} WHERE {not_null}
             ) d
             WHERE EXISTS (SELECT 1 FROM {parent} p WHERE {join_d})) AS MATCHED,
            (SELECT COUNT(*) FROM {child} c
              WHERE {not_null}
                AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join_c})) AS ORPHAN_ROWS
        FROM dual
    """  # noqa: S608 - all identifiers come from the Oracle data dictionary, not user input
    try:
        row = db.query(sql)[0]
    except Exception:  # noqa: BLE001 - overlap is best-effort, never fatal
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
