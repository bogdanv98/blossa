# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""The Blossa web app: a thin FastAPI server over the existing map + NL→SQL pipeline.

Three endpoints, mirroring the CLI's trust/safety model exactly:

  GET  /api/map   -> the scanned database map (schema + meanings + relationships) for the browser.
  POST /api/ask   -> turn a natural-language question into ONE read-only SELECT (proposal only;
                     the model sees only the PII-safe map, never raw rows). Does NOT execute.
  POST /api/run   -> validate a SELECT as read-only, row-cap it, run it over a READ ONLY
                     connection, and return the rows (shown to the user only, never to the model).

`create_app` takes the loaded settings + report and optional `provider` / `db_factory` injectors so
the endpoints can be tested without a live model or database.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Settings
from ..db.connection import Database
from ..llm import get_provider
from ..llm.base import LLMProvider
from ..models import ScanReport
from ..nlquery import (
    ASK_SYSTEM_PROMPT,
    UnsafeQueryError,
    build_ask_prompt,
    parse_ask_response,
    privilege_hint,
    validate_read_only_select,
    with_row_limit,
)

_STATIC_DIR = Path(__file__).parent / "static"


def _distinct_owners(report: ScanReport) -> list[str]:
    owners: list[str] = []
    for t in report.schema_info.tables:
        if t.owner and t.owner not in owners:
            owners.append(t.owner)
    return owners


def build_map_view(report: ScanReport) -> dict:
    """A UI-tailored view of the map: per-table columns (with computed type, key role, meaning) and
    relationships split into out/in, plus findings. Built server-side so the frontend stays dumb."""
    multi = len(_distinct_owners(report)) > 1

    def q(name: str, owner: str | None) -> str:
        return f"{owner}.{name}" if (multi and owner) else name

    def rel_label(r) -> str:
        src = q(r.from_table, r.from_owner)
        dst = q(r.to_table, r.to_owner)
        kind = "declared" if r.declared else f"inferred · {r.confidence.value}"
        cross = " · cross-schema" if r.cross_schema else ""
        return (
            f"{src}({', '.join(r.from_columns)}) → {dst}({', '.join(r.to_columns)}) "
            f"[{kind}{cross}]"
        )

    tables = []
    for table in report.schema_info.tables:
        sem = report.semantics_for(table.name)
        col_sem = {c.column.upper(): c for c in sem.columns} if sem else {}
        pk_cols = set(table.primary_key.columns) if table.primary_key else set()
        fk_cols = {c for fk in table.foreign_keys for c in fk.columns}
        columns = []
        for col in table.columns:
            cs = col_sem.get(col.name.upper())
            columns.append(
                {
                    "name": col.name,
                    "type": col.type_signature,
                    "key": "PK" if col.name in pk_cols else ("FK" if col.name in fk_cols else ""),
                    "nullable": col.nullable,
                    "meaning": cs.meaning if cs else "",
                    "confidence": cs.confidence.value if cs else "",
                    "comment": col.comment or "",
                }
            )
        tables.append(
            {
                "name": q(table.name, table.owner),
                "owner": table.owner,
                "num_rows": table.num_rows,
                "comment": table.comment or "",
                "purpose": sem.purpose if sem else "",
                "purpose_confidence": sem.confidence.value if sem else "",
                "columns": columns,
                "references_out": [
                    rel_label(r) for r in report.relationships if r.from_table == table.name
                ],
                "references_in": [
                    rel_label(r) for r in report.relationships if r.to_table == table.name
                ],
                "findings": [f.message for f in report.findings_for(table.name)],
            }
        )

    return {
        "schema_name": report.metadata.schema_name,
        "multi_schema": multi,
        "table_count": report.metadata.table_count,
        "provider": report.metadata.llm_provider,
        "tables": tables,
    }


class AskBody(BaseModel):
    question: str


class RunBody(BaseModel):
    sql: str
    max_rows: int = 100


def create_app(
    settings: Settings,
    report: ScanReport,
    *,
    provider: LLMProvider | None = None,
    db_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="Blossa", docs_url=None, redoc_url=None)
    make_db = db_factory or (lambda: Database(settings.oracle))
    state: dict[str, LLMProvider | None] = {"provider": provider}

    def _ensure_provider() -> LLMProvider:
        if settings.llm.provider == "heuristic":
            raise HTTPException(
                status_code=400,
                detail="Asking questions needs a model provider (ollama or openai_compatible); "
                "the offline heuristic can't translate language to SQL.",
            )
        if state["provider"] is None:
            state["provider"] = get_provider(settings.llm)
        return state["provider"]

    @app.get("/api/map")
    def get_map() -> dict:
        return build_map_view(report)

    @app.post("/api/ask")
    def post_ask(body: AskBody) -> dict:
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question.")
        prov = _ensure_provider()
        prompt = build_ask_prompt(question, report, use_dba=settings.oracle.use_dba_catalog)
        try:
            raw = prov.generate(ASK_SYSTEM_PROMPT, prompt)
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
            raise HTTPException(status_code=502, detail=f"The model call failed: {exc}") from exc
        return parse_ask_response(raw).model_dump()

    @app.post("/api/run")
    def post_run(body: RunBody) -> dict:
        try:
            safe_sql = validate_read_only_select(body.sql)
        except UnsafeQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            with make_db() as db:
                rows = db.query(with_row_limit(safe_sql, body.max_rows))
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
            detail = f"Query failed: {exc}"
            hint = privilege_hint(safe_sql, str(exc))
            if hint:
                detail += f"  {hint}"
            raise HTTPException(status_code=502, detail=detail) from exc
        columns = list(rows[0].keys()) if rows else []
        return {
            "columns": columns,
            "rows": [list(r.values()) for r in rows],
            "row_count": len(rows),
            "capped": len(rows) >= body.max_rows,
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
