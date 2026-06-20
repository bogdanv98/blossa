# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Assemble PII-safe TableSummary objects — the single artifact the LLM is allowed to see.

A summary carries structure (columns, types, keys), relationships (declared + inferred), and
aggregate profile facts (distinct counts, null fractions, value *patterns*, *masked* samples).
By construction it contains no raw row values, so it is safe to send to any LLM provider.
"""

from __future__ import annotations

from ..models import (
    ColumnInfo,
    ColumnSummary,
    KeyRole,
    Relationship,
    SchemaInfo,
    TableInfo,
    TableSummary,
)


def build_summaries(
    schema: SchemaInfo, relationships: list[Relationship]
) -> list[TableSummary]:
    outbound, inbound = _index_relationships(relationships)
    return [
        _build_table_summary(table, outbound.get(table.name, []), inbound.get(table.name, []))
        for table in schema.tables
    ]


def _index_relationships(
    relationships: list[Relationship],
) -> tuple[dict[str, list[Relationship]], dict[str, list[Relationship]]]:
    outbound: dict[str, list[Relationship]] = {}
    inbound: dict[str, list[Relationship]] = {}
    for rel in relationships:
        outbound.setdefault(rel.from_table, []).append(rel)
        inbound.setdefault(rel.to_table, []).append(rel)
    return outbound, inbound


def _build_table_summary(
    table: TableInfo, outbound: list[Relationship], inbound: list[Relationship]
) -> TableSummary:
    pk_cols = set(table.primary_key.columns) if table.primary_key else set()
    fk_targets = {
        col: f"{fk.referenced_table}.{ref}"
        for fk in table.foreign_keys
        for col, ref in zip(fk.columns, fk.referenced_columns, strict=False)
    }
    # Inferred (undeclared) FK targets enrich the "references" hint too.
    for rel in outbound:
        if not rel.declared:
            for col, ref in zip(rel.from_columns, rel.to_columns, strict=False):
                fk_targets.setdefault(col, f"{rel.to_table}.{ref} (inferred)")
    fk_cols = set(fk_targets)

    columns = [
        _build_column_summary(table, col, pk_cols, fk_cols, fk_targets) for col in table.columns
    ]
    return TableSummary(
        name=table.name,
        comment=table.comment,
        row_count=table.num_rows,
        columns=columns,
        outbound=outbound,
        inbound=inbound,
    )


def _build_column_summary(
    table: TableInfo,
    col: ColumnInfo,
    pk_cols: set[str],
    fk_cols: set[str],
    fk_targets: dict[str, str],
) -> ColumnSummary:
    role = _key_role(col.name, pk_cols, fk_cols)
    profile = table.profiles.get(col.name)
    return ColumnSummary(
        name=col.name,
        type=col.type_signature,
        nullable=col.nullable,
        key_role=role,
        comment=col.comment,
        references=fk_targets.get(col.name),
        distinct_count=profile.distinct_count if profile else None,
        null_fraction=round(profile.null_fraction, 4) if profile else None,
        value_patterns=profile.value_patterns if profile else [],
        masked_samples=profile.masked_samples if profile else [],
    )


def _key_role(name: str, pk_cols: set[str], fk_cols: set[str]) -> KeyRole:
    is_pk, is_fk = name in pk_cols, name in fk_cols
    if is_pk and is_fk:
        return KeyRole.PK_AND_FK
    if is_pk:
        return KeyRole.PRIMARY_KEY
    if is_fk:
        return KeyRole.FOREIGN_KEY
    return KeyRole.NONE
