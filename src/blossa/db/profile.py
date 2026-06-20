# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""PII-safe data profiling.

For each table we run *aggregate* SQL (row counts, null counts, distinct counts) and pull a
small bounded sample of rows. Sampled raw values are converted to structural patterns and
masked samples immediately (see `blossa.privacy`) and then discarded — no raw value is stored
on the model or returned. This is the only place Blossa reads table *data* (still read-only).
"""

from __future__ import annotations

from ..models import ColumnInfo, ColumnProfile, TableInfo
from ..privacy import masked_samples, summarize_patterns
from .connection import QueryExecutor

# Scalar types we can safely aggregate and sample. LOB/LONG/RAW/XML are skipped.
_PROFILABLE_PREFIXES = (
    "NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
    "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR",
    "DATE", "TIMESTAMP",
)
_LOW_CARDINALITY_MAX = 25


def _is_profilable(col: ColumnInfo) -> bool:
    t = col.data_type.upper()
    return any(t.startswith(p) for p in _PROFILABLE_PREFIXES)


def profile_table(db: QueryExecutor, owner: str, table: TableInfo, sample_rows: int) -> None:
    """Compute a PII-safe ColumnProfile for each profilable column and attach to `table`."""
    cols = [c for c in table.columns if _is_profilable(c)]
    if not cols:
        return

    qualified = f'"{owner}"."{table.name}"'
    total, aggregates = _aggregate(db, qualified, cols)

    # Initialise profiles from the aggregate pass.
    for i, col in enumerate(cols):
        non_null = aggregates.get(f"NN_{i}", 0) or 0
        distinct = aggregates.get(f"DC_{i}", 0) or 0
        table.profiles[col.name] = ColumnProfile(
            total_rows=total,
            null_count=max(total - non_null, 0),
            distinct_count=distinct,
            is_low_cardinality=(0 < distinct <= _LOW_CARDINALITY_MAX and distinct < max(total, 1)),
        )

    if total == 0:
        return

    # Sample pass: derive patterns + masked samples + observed lengths, then discard raw values.
    _enrich_from_sample(db, qualified, cols, table, sample_rows)


def _aggregate(
    db: QueryExecutor, qualified: str, cols: list[ColumnInfo]
) -> tuple[int, dict[str, int]]:
    selects = ["COUNT(*) AS TOTAL"]
    for i, col in enumerate(cols):
        c = f'"{col.name}"'
        selects.append(f"COUNT({c}) AS NN_{i}")
        selects.append(f"COUNT(DISTINCT {c}) AS DC_{i}")
    sql = f"SELECT {', '.join(selects)} FROM {qualified}"  # noqa: S608 - identifiers are from the DD
    rows = db.query(sql)
    row = rows[0] if rows else {}
    total = int(row.get("TOTAL", 0) or 0)
    aggregates = {k: int(v) for k, v in row.items() if k != "TOTAL" and v is not None}
    return total, aggregates


def _enrich_from_sample(
    db: QueryExecutor,
    qualified: str,
    cols: list[ColumnInfo],
    table: TableInfo,
    sample_rows: int,
) -> None:
    col_list = ", ".join(f'"{c.name}"' for c in cols)
    sql = f"SELECT {col_list} FROM {qualified} WHERE ROWNUM <= :n"  # noqa: S608 - DD identifiers
    sample = db.query(sql, {"n": max(sample_rows, 1)})

    for col in cols:
        values = [r[col.name] for r in sample if r.get(col.name) is not None]
        if not values:
            continue
        texts = [str(v) for v in values]
        lengths = [len(t) for t in texts]
        profile = table.profiles[col.name]
        profile.min_length = min(lengths)
        profile.max_length = max(lengths)
        profile.value_patterns = summarize_patterns(texts)
        profile.masked_samples = masked_samples(texts)
