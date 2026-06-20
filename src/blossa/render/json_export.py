# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Machine-readable JSON artifact — the full ScanReport, losslessly."""

from __future__ import annotations

from pathlib import Path

from ..models import ScanReport


def render_json(report: ScanReport) -> str:
    return report.model_dump_json(indent=2)


def write_json(report: ScanReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(report), encoding="utf-8")
    return out
