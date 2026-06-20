# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Render the ScanReport to a Markdown "database map" using a Jinja2 template."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import ConfidenceLevel, ScanReport

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_CONFIDENCE_BADGE = {
    ConfidenceLevel.HIGH: "🟢 high",
    ConfidenceLevel.MEDIUM: "🟡 medium",
    ConfidenceLevel.LOW: "🔴 low",
}


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["confidence"] = lambda c: _CONFIDENCE_BADGE.get(c, str(c))
    return env


def render_markdown(report: ScanReport) -> str:
    env = _make_env()
    template = env.get_template("database_map.md.j2")
    return template.render(
        report=report,
        meta=report.metadata,
        schema=report.schema_info,
        relationships=report.relationships,
        findings=report.findings,
    )


def write_markdown(report: ScanReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    return out
