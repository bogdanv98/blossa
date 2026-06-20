# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Offline, rule-based "semantic" provider.

No model, no network. It infers meaning from names, types, keys, relationships and value
patterns using conservative heuristics. It exists so the full pipeline runs end-to-end without
Ollama/GPU (CI, first run, the `--demo` path) — and as an honest baseline. Its guesses are
deliberately labelled with modest confidence.
"""

from __future__ import annotations

from ..models import (
    ColumnSemantics,
    ColumnSummary,
    ConfidenceLevel,
    KeyRole,
    TableSemantics,
    TableSummary,
)
from .base import LLMProvider

# token (matched against underscore-split column-name parts) -> (meaning, confidence)
_HIGH, _MED = ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM
_TOKEN_MEANINGS: list[tuple[tuple[str, ...], str, ConfidenceLevel]] = [
    (("EMAIL", "MAIL"), "an email address", _HIGH),
    (("PHONE", "TEL", "MOBILE"), "a phone number", _HIGH),
    (("NAME", "FNAME", "LNAME"), "a name / label", _MED),
    (("DESCR", "DESC", "DESCRIPTION", "TXT", "TEXT", "NOTE"), "free-text description", _MED),
    (("QTY", "QUANTITY", "COUNT"), "a quantity / count", _MED),
    (("PRICE", "AMT", "AMOUNT", "TOTAL", "COST", "SUM"), "a monetary amount", _MED),
    (("DATE", "DT", "TS", "TIME", "CREATED", "UPDATED", "MODIFIED"), "a date / timestamp", _MED),
    (("STATUS", "STAT", "STATE", "FLG", "FLAG"), "a status / flag", _MED),
    (("CD", "CODE", "CAT", "TYPE", "KIND"), "a classification code", _MED),
    (("SKU",), "a stock-keeping unit code", _MED),
]


class HeuristicProvider(LLMProvider):
    name = "heuristic"
    model = None

    def analyze(self, summary: TableSummary) -> TableSemantics:
        columns = [self._column_semantics(summary, c) for c in summary.columns]
        purpose, confidence, evidence = self._table_purpose(summary)
        return TableSemantics(
            table=summary.name,
            purpose=purpose,
            confidence=confidence,
            evidence=evidence,
            columns=columns,
        )

    # ---------------------------------------------------------------- columns

    def _column_semantics(self, summary: TableSummary, col: ColumnSummary) -> ColumnSemantics:
        if col.comment and col.comment.strip():
            return ColumnSemantics(
                column=col.name,
                meaning=col.comment.strip(),
                confidence=ConfidenceLevel.HIGH,
                evidence=["Documented by an existing column comment."],
            )

        if col.key_role in (KeyRole.PRIMARY_KEY, KeyRole.PK_AND_FK):
            entity = self._singular(summary.name)
            return ColumnSemantics(
                column=col.name,
                meaning=f"Primary key — unique identifier for a {entity} row.",
                confidence=ConfidenceLevel.HIGH,
                evidence=["Part of the primary key."],
            )
        if col.key_role == KeyRole.FOREIGN_KEY and col.references:
            inferred = " (inferred relationship)" if "inferred" in col.references else ""
            target = col.references.replace(" (inferred)", "")
            return ColumnSemantics(
                column=col.name,
                meaning=f"Foreign key referencing {target}{inferred}.",
                confidence=ConfidenceLevel.HIGH if not inferred else ConfidenceLevel.MEDIUM,
                evidence=[f"References {col.references}."],
            )

        parts = set(col.name.upper().split("_"))
        for tokens, meaning, conf in _TOKEN_MEANINGS:
            matched = parts.intersection(tokens)
            if matched:
                evidence = [f"Column name contains '{next(iter(matched))}'."]
                if col.value_patterns:
                    evidence.append(f"Observed value pattern(s): {', '.join(col.value_patterns)}.")
                return ColumnSemantics(
                    column=col.name,
                    meaning=f"Likely {meaning}.",
                    confidence=conf,
                    evidence=evidence,
                )

        if col.name.upper().endswith(("_ID", "_KEY", "_NO", "_NUM")):
            return ColumnSemantics(
                column=col.name,
                meaning="Likely an identifier or reference number.",
                confidence=ConfidenceLevel.LOW,
                evidence=["Name ends with an identifier-like suffix."],
            )

        return ColumnSemantics(
            column=col.name,
            meaning="Meaning unclear from structure alone (low-confidence guess).",
            confidence=ConfidenceLevel.LOW,
            evidence=["No strong naming, key or pattern signal."],
        )

    # ------------------------------------------------------------------ table

    def _table_purpose(
        self, summary: TableSummary
    ) -> tuple[str, ConfidenceLevel, list[str]]:
        if summary.comment and summary.comment.strip():
            return (summary.comment.strip(), ConfidenceLevel.HIGH, ["Existing table comment."])

        n_fk = sum(1 for c in summary.columns if c.key_role == KeyRole.FOREIGN_KEY)
        inbound = len(summary.inbound)
        singular = self._singular(summary.name)

        # A table that is mostly foreign keys looks like a link / line-item / transaction table.
        if n_fk >= 2 and n_fk >= len(summary.columns) // 2:
            return (
                f"Likely a transactional / associative table linking "
                f"{', '.join(sorted({r.to_table for r in summary.outbound})) or 'other entities'}.",
                ConfidenceLevel.MEDIUM,
                [f"{n_fk} of {len(summary.columns)} columns are foreign keys."],
            )
        # Heavily referenced by others → a master / reference entity.
        if inbound >= 1:
            return (
                f"Likely a master/reference entity ('{singular}') referenced by other tables.",
                ConfidenceLevel.MEDIUM,
                [f"Referenced by {inbound} relationship(s) from other tables."],
            )
        if len(summary.columns) <= 3 and any(
            c.key_role == KeyRole.PRIMARY_KEY for c in summary.columns
        ):
            return (
                f"Likely a small lookup / code table describing '{singular}' values.",
                ConfidenceLevel.LOW,
                ["Few columns with a primary key — typical of a lookup table."],
            )
        return (
            f"Likely stores '{singular}' records (purpose inferred from the table name only).",
            ConfidenceLevel.LOW,
            ["Inferred from the table name; little other structural signal."],
        )

    @staticmethod
    def _singular(table_name: str) -> str:
        name = table_name.lower().replace("_", " ")
        return name[:-1] if name.endswith("s") else name
