# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Drive the LLM provider over each PII-safe table summary.

Each table is analysed independently. A failure on one table (model error, timeout) degrades to
a clearly-labelled low-confidence result for that table only — the scan never aborts because a
single inference failed.
"""

from __future__ import annotations

from collections.abc import Callable

from ..llm.base import LLMProvider
from ..models import (
    ColumnSemantics,
    ConfidenceLevel,
    TableSemantics,
    TableSummary,
)

ProgressFn = Callable[[str, int, int], None]


def run_semantic_pass(
    provider: LLMProvider,
    summaries: list[TableSummary],
    progress: ProgressFn | None = None,
) -> list[TableSemantics]:
    results: list[TableSemantics] = []
    total = len(summaries)
    for i, summary in enumerate(summaries, start=1):
        if progress:
            progress(summary.name, i, total)
        try:
            results.append(provider.analyze(summary))
        except Exception as exc:  # noqa: BLE001 - one table's failure must not kill the scan
            results.append(_error_semantics(summary, exc))
    return results


def _error_semantics(summary: TableSummary, exc: Exception) -> TableSemantics:
    reason = f"{type(exc).__name__}: {exc}"
    return TableSemantics(
        table=summary.name,
        purpose=f"Semantic inference failed for this table ({reason}).",
        confidence=ConfidenceLevel.LOW,
        evidence=[reason],
        columns=[
            ColumnSemantics(
                column=c.name,
                meaning="Not inferred (inference failed for this table).",
                confidence=ConfidenceLevel.LOW,
            )
            for c in summary.columns
        ],
    )
