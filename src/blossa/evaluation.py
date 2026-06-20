# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Evaluate Blossa against a documented schema used as ground truth.

The idea: take a schema that *is* documented (e.g. an Oracle sample schema), capture its real
comments + foreign keys as "ground truth", then strip the docs and drop the FK declarations to
simulate a legacy estate. Run `blossa scan` on the stripped schema and measure how much Blossa
recovers.

Two kinds of metric:
  * FK rediscovery (objective): of the real foreign keys we hid, how many did Blossa re-infer as
    candidate relationships? Reported as recall + precision.
  * Documentation coverage (proxy): of the tables/columns that *had* a real comment, for how many
    did Blossa produce a non-low-confidence meaning? (Whether the meaning is *correct* still needs
    a human or an LLM judge; this measures whether the gap was filled at all.)
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import ConfidenceLevel, ScanReport, SchemaInfo

# --------------------------------------------------------------- ground truth


def build_ground_truth(schema: SchemaInfo) -> dict:
    """Capture real comments + declared FKs from a (still documented) schema."""
    tables: dict[str, dict] = {}
    for table in schema.tables:
        col_comments = {
            c.name: c.comment for c in table.columns if c.comment and c.comment.strip()
        }
        fks = [
            {
                "from_columns": fk.columns,
                "to_table": fk.referenced_table,
                "to_columns": fk.referenced_columns,
            }
            for fk in table.foreign_keys
            if fk.referenced_table
        ]
        tables[table.name] = {
            "comment": (table.comment or "").strip() or None,
            "columns": col_comments,
            "foreign_keys": fks,
        }
    return {"schema": schema.name, "tables": tables}


# ------------------------------------------------------------------- results


class FKMetrics(BaseModel):
    truth_total: int = 0
    found_total: int = 0
    matched: int = 0
    missed: list[str] = Field(default_factory=list)  # real FKs Blossa did not rediscover

    @property
    def recall(self) -> float:
        return self.matched / self.truth_total if self.truth_total else 0.0

    @property
    def precision(self) -> float:
        return self.matched / self.found_total if self.found_total else 0.0


class CoverageMetrics(BaseModel):
    documented: int = 0  # items that had a real comment in ground truth
    recovered: int = 0  # of those, items Blossa gave a >= medium-confidence meaning

    @property
    def coverage(self) -> float:
        return self.recovered / self.documented if self.documented else 0.0


class EvalResult(BaseModel):
    schema_name: str
    fk: FKMetrics
    table_docs: CoverageMetrics
    column_docs: CoverageMetrics


# ----------------------------------------------------------------- evaluate

_GOOD_CONFIDENCE = {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH}


def evaluate(ground_truth: dict, report: ScanReport) -> EvalResult:
    truth_tables: dict = ground_truth.get("tables", {})

    return EvalResult(
        schema_name=ground_truth.get("schema", report.metadata.schema_name),
        fk=_fk_metrics(truth_tables, report),
        table_docs=_table_doc_coverage(truth_tables, report),
        column_docs=_column_doc_coverage(truth_tables, report),
    )


def _fk_key(from_table: str, from_columns: list[str], to_table: str) -> tuple:
    return (from_table.upper(), frozenset(c.upper() for c in from_columns), to_table.upper())


def _fk_metrics(truth_tables: dict, report: ScanReport) -> FKMetrics:
    truth: set[tuple] = set()
    for table_name, info in truth_tables.items():
        for fk in info.get("foreign_keys", []):
            if fk.get("to_table"):
                truth.add(_fk_key(table_name, fk["from_columns"], fk["to_table"]))

    # What Blossa knows about: declared FKs still present + inferred candidate FKs.
    found = {
        _fk_key(r.from_table, r.from_columns, r.to_table) for r in report.relationships
    }

    matched = truth & found
    missed = sorted(
        f"{t}({','.join(sorted(cols))}) -> {dst}" for (t, cols, dst) in (truth - found)
    )
    return FKMetrics(
        truth_total=len(truth),
        found_total=len(found),
        matched=len(matched),
        missed=missed,
    )


def _table_doc_coverage(truth_tables: dict, report: ScanReport) -> CoverageMetrics:
    documented = recovered = 0
    for table_name, info in truth_tables.items():
        if not info.get("comment"):
            continue
        documented += 1
        sem = report.semantics_for(table_name)
        if sem and sem.confidence in _GOOD_CONFIDENCE:
            recovered += 1
    return CoverageMetrics(documented=documented, recovered=recovered)


def _column_doc_coverage(truth_tables: dict, report: ScanReport) -> CoverageMetrics:
    documented = recovered = 0
    for table_name, info in truth_tables.items():
        sem = report.semantics_for(table_name)
        by_col = {c.column.upper(): c for c in sem.columns} if sem else {}
        for col_name in info.get("columns", {}):
            documented += 1
            col_sem = by_col.get(col_name.upper())
            if col_sem and col_sem.confidence in _GOOD_CONFIDENCE:
                recovered += 1
    return CoverageMetrics(documented=documented, recovered=recovered)
