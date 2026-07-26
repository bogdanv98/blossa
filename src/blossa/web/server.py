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

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..codegen import (
    CODEGEN_SYSTEM_PROMPT,
    build_codegen_prompt,
    is_code_request,
    parse_codegen_response,
)
from ..config import Settings
from ..db.connection import Database
from ..ddl import (
    GET_DDL_SQL,
    clob_text,
    metadata_type,
    offline_ddl,
    split_qualified,
    validate_identifier,
)
from ..llm import get_provider
from ..llm.base import LLMProvider
from ..logsense import (
    LOCAL_PROVIDERS,
    TIME_GRAINS,
    build_spike_report,
    choose_log_table,
    local_only_message,
    parse_since,
    recent_entries_sql,
    redact_entries,
    run_root_cause,
    source_time_bucket_sql,
    time_bucket_sql,
)
from ..models import LogRole, ProgramKind, ScanReport
from ..nlquery import (
    ASK_SYSTEM_PROMPT,
    Turn,
    UnsafeQueryError,
    answer_program_lookup,
    build_ask_prompt,
    catalog_is_readable,
    enforce_error_severity_filter,
    expand_count_to_list,
    parse_ask_response,
    privilege_hint,
    validate_read_only_select,
    with_row_limit,
)
from ..program import declared_subprograms

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """A token that changes whenever a static asset changes.

    The assets carry no expiry of their own, so a browser is free to reuse app.js out of its
    heuristic cache and pair an old UI with a newer server — which is exactly how a shipped tab
    can fail to appear. Stamping this into the asset URLs makes that impossible, and keying it on
    file mtime+size (not the release version) means it also turns over during development.
    """
    parts = [__version__]
    for name in ("app.js", "style.css"):
        try:
            st = (_STATIC_DIR / name).stat()
            parts.append(f"{name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:  # asset missing: fall back to the version alone
            parts.append(name)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


class _RevalidatingStatic(StaticFiles):
    """Serve assets with `Cache-Control: no-cache` — revalidate, don't blindly reuse.

    The ETag is still honoured, so an unchanged file costs one 304 rather than a re-download.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Object types the map already describes in detail (tables + program units), so the browser's
# catch-all "other objects" list doesn't duplicate them.
_RICH_OBJECT_TYPES = {"TABLE", "VIEW", "PACKAGE", "PROCEDURE", "FUNCTION", "TRIGGER"}


def _distinct_owners(report: ScanReport) -> list[str]:
    owners: list[str] = []
    for t in report.schema_info.tables:
        if t.owner and t.owner not in owners:
            owners.append(t.owner)
    return owners


def _default_owner(report: ScanReport) -> str | None:
    """The owner to assume when the UI sends a bare object name (single-schema maps)."""
    owners = _distinct_owners(report)
    return owners[0] if len(owners) == 1 else None


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

    # Object browser list: the authoritative set is the captured program_units (present even when
    # the scan ran without a model), each carrying its source/DDL. We left-join the LLM's summary
    # from program_semantics when one exists, so the tree can show every view/package/proc/etc.
    prog_sem = {(ps.owner, ps.name, ps.kind): ps for ps in report.program_semantics}
    obj_status = {
        ((o.owner or "").upper(), o.name.upper(), o.type.upper()): o.status
        for o in report.schema_info.objects
    }
    programs = [
        {
            "name": q(u.name, u.owner),
            "owner": u.owner,
            "kind": u.kind.value,
            "summary": ps.summary if ps else "",
            "tables_used": ps.tables_used if ps else [],
            "confidence": ps.confidence.value if ps else "",
            "source": u.source or "",
            # VALID/INVALID from the catalog — an invalid package is worth seeing in the tree.
            "status": obj_status.get(((u.owner or "").upper(), u.name.upper(), u.kind.value), ""),
            # What a package declares: these routines exist only inside it, never in the catalog,
            # so the tree is the only place a user can discover them. "does" joins in the
            # per-routine sentence captured by the package's own explain call, when one ran.
            "subprograms": (
                [
                    {"name": n, "kind": k.value, "does": ps.routine_summary(n) if ps else ""}
                    for n, k in declared_subprograms(u.source)
                ]
                if u.kind == ProgramKind.PACKAGE
                else []
            ),
        }
        for u in report.schema_info.program_units
        for ps in [prog_sem.get((u.owner, u.name, u.kind))]
    ]

    # The rest of the inventory: sequences, synonyms, materialized views, types, indexes. Tables
    # and program units already have richer entries above, so they are not repeated here.
    other_objects = [
        {"name": q(o.name, o.owner), "owner": o.owner, "type": o.type, "status": o.status}
        for o in report.schema_info.objects
        if o.type.upper() not in _RICH_OBJECT_TYPES
    ]

    log_tables = [
        {
            "name": q(lt.table, lt.owner),
            "owner": lt.owner,
            "kind": lt.kind.value,
            "confidence": lt.confidence.value,
            "columns": [{"column": c.column, "role": c.role.value} for c in lt.columns],
            "evidence": lt.evidence,
        }
        for lt in report.log_tables
    ]

    # Scheduled processes. A step names a program, the program names a procedure — resolved during
    # introspection — so the UI can show the batch's order and let a reader jump from a step to the
    # packaged routine the Logic tab already explains.
    # Keyed on the unqualified unit name: a step's action names the package, and the same package
    # name cannot repeat within one owner. Falls back across owners for single-schema maps.
    sem_by_unit = {ps.name.upper(): ps for ps in report.program_semantics}

    def step_view(step) -> dict:
        # "BANKDEMO.EOD_BATCH.S04_SETTLE_PENDING" -> package EOD_BATCH, routine S04_SETTLE_PENDING.
        parts = [p.strip('"') for p in step.action.split(".")] if step.action else []
        package = parts[-2].upper() if len(parts) >= 2 else ""
        routine = parts[-1].upper() if parts else ""
        ps = sem_by_unit.get(package) if package else None
        summary = ps.routine_summary(routine) if ps else ""
        return {
            "name": step.name,
            "program": q(step.program_name, step.program_owner) if step.program_name else "",
            "action": step.action,
            "package": package,
            "routine": routine,
            "does": summary,
        }

    scheduler_chains = [
        {
            "name": q(chain.name, chain.owner),
            "owner": chain.owner,
            "enabled": chain.enabled,
            "comment": chain.comment or "",
            "steps": [step_view(s) for s in chain.steps],
            "rules": [
                {
                    "name": r.name,
                    "condition": r.condition,
                    "action": r.action,
                    "comment": r.comment or "",
                }
                for r in chain.rules
            ],
        }
        for chain in report.schema_info.scheduler_chains
    ]

    scheduler_jobs = [
        {
            "name": q(job.name, job.owner),
            "owner": job.owner,
            "job_type": job.job_type,
            "job_action": job.job_action,
            "runs_chain": job.runs_chain,
            "repeat_interval": job.repeat_interval,
            "enabled": job.enabled,
            "state": job.state,
            "restartable": job.restartable,
            "last_start": job.last_start,
            "next_run": job.next_run,
            "comment": job.comment or "",
        }
        for job in report.schema_info.scheduler_jobs
    ]

    return {
        "schema_name": report.metadata.schema_name,
        "multi_schema": multi,
        "table_count": report.metadata.table_count,
        "provider": report.metadata.llm_provider,
        "tables": tables,
        "programs": programs,
        "other_objects": other_objects,
        "log_tables": log_tables,
        "scheduler_chains": scheduler_chains,
        "scheduler_jobs": scheduler_jobs,
    }


class AskBody(BaseModel):
    question: str
    # Prior turns (question + the model's own SQL), so a follow-up can refine the last query.
    # Never carries results — the no-raw-rows-to-the-model boundary holds across turns.
    history: list[Turn] = Field(default_factory=list)


class RunBody(BaseModel):
    sql: str
    max_rows: int = 100


class DdlBody(BaseModel):
    name: str  # bare or "OWNER.OBJECT" (the UI qualifies names in multi-schema maps)
    type: str = "TABLE"  # Oracle OBJECT_TYPE: TABLE / VIEW / PACKAGE / SEQUENCE / ...
    owner: str | None = None


class ExplainLogBody(BaseModel):
    table: str | None = None  # which log table; None = auto-pick the main error log
    limit: int = 50


class SpikesBody(BaseModel):
    table: str | None = None  # which log table; None = auto-pick the main error log
    grain: str = "hour"  # hour | day
    since: str | None = None  # e.g. "48h" / "7d"; None = whole history
    only_errors: bool = True


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

    # The map is immutable for the lifetime of the server, and at scale the view is expensive to
    # rebuild AND to re-serialize (4000 tables -> ~4 MB, ~3 s per request). Both are paid once.
    map_json_cache: dict[str, str] = {}

    @app.get("/api/map")
    def get_map() -> Response:
        if "json" not in map_json_cache:
            map_json_cache["json"] = json.dumps(build_map_view(report), default=str)
        return Response(content=map_json_cache["json"], media_type="application/json")

    @app.post("/api/ask")
    def post_ask(body: AskBody) -> dict:
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question.")
        # Three kinds of answer share this one box. "Write me a procedure like get_balance" is a
        # request to BUILD something — it produces code, never a query, and Blossa does not run it.
        if is_code_request(question):
            prov = _ensure_provider()
            history_text = " ".join(t.question for t in body.history)
            prompt = build_codegen_prompt(
                question,
                report,
                language=settings.llm.language,
                history_text=history_text,
            )
            try:
                raw = prov.generate(CODEGEN_SYSTEM_PROMPT, prompt)
            except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
                raise HTTPException(
                    status_code=502, detail=f"The model call failed: {exc}"
                ) from exc
            return {"kind": "code", **parse_codegen_response(raw, report).model_dump()}
        # "Is there a get_balance procedure?" is answerable from the map alone — and answering it
        # there is both correct and instant, where a generated catalog query can be neither.
        known = answer_program_lookup(
            question, report, use_dba=settings.oracle.use_dba_catalog
        )
        if known is not None:
            return {"kind": "sql", **known.model_dump()}
        prov = _ensure_provider()
        prompt = build_ask_prompt(
            question, report, use_dba=settings.oracle.use_dba_catalog, history=body.history
        )
        try:
            raw = prov.generate(ASK_SYSTEM_PROMPT, prompt)
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
            raise HTTPException(status_code=502, detail=f"The model call failed: {exc}") from exc
        result = enforce_error_severity_filter(question, parse_ask_response(raw), report)
        result = expand_count_to_list(question, result)
        return {"kind": "sql", **result.model_dump()}

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
            # Probe on a fresh connection so the hint can tell a missing privilege from a view
            # name that does not exist. Only on the error path.
            readable: bool | None = None
            try:
                with make_db() as probe:
                    readable = catalog_is_readable(probe)
            except Exception:  # noqa: BLE001 - cannot probe: the hint says "unknown" instead
                readable = None
            hint = privilege_hint(safe_sql, str(exc), readable)
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

    @app.post("/api/ddl")
    def post_ddl(body: DdlBody) -> dict:
        # Structure, not rows: the same side of the boundary as the map itself.
        qualified_owner, object_name = split_qualified(body.name)
        try:
            object_name = validate_identifier(object_name, "object")
            owner = body.owner or qualified_owner or _default_owner(report)
            owner = validate_identifier(owner, "schema") if owner else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        otype = metadata_type(body.type)
        ddl, origin, note = "", "", ""
        if otype and owner:
            try:
                with make_db() as db:
                    rows = db.query(
                        GET_DDL_SQL, {"otype": otype, "name": object_name, "owner": owner}
                    )
                    # GET_DDL hands back a LOB locator, so it must be read while the connection
                    # is still open — outside the block it fails with DPY-1001.
                    ddl = clob_text(rows[0].get("DDL") if rows else "").strip()
                origin = "database"
            except Exception as exc:  # noqa: BLE001 - fall back to the map rather than fail
                note = (
                    f"Oracle would not hand over the DDL ({exc}); "
                    "showing what the scan captured."
                )
        if not ddl:
            ddl = offline_ddl(report, owner, object_name, body.type)
            origin = "scan"
        if not ddl:
            raise HTTPException(
                status_code=404,
                detail=note or f"No DDL available for {body.name}. Re-scan to capture it.",
            )
        return {"name": body.name, "type": body.type.upper(), "ddl": ddl,
                "source": origin, "note": note}

    @app.post("/api/logs/explain")
    def post_explain_log(body: ExplainLogBody) -> dict:
        # Reads real error text → only allowed with a LOCAL model, where data never leaves the box.
        if settings.llm.provider == "heuristic":
            raise HTTPException(
                status_code=400,
                detail="Explaining errors needs a model provider (the offline heuristic can't "
                "read error text).",
            )
        if settings.llm.provider not in LOCAL_PROVIDERS:
            raise HTTPException(status_code=400, detail=local_only_message())
        lt = choose_log_table(report.log_tables, body.table)
        if lt is None:
            raise HTTPException(status_code=404, detail="No matching log table in this map.")
        prov = _ensure_provider()
        sql = recent_entries_sql(lt, limit=body.limit, only_errors=True)
        try:
            with make_db() as db:
                rows = db.query(sql)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Log query failed: {exc}") from exc
        redacted = redact_entries(rows, lt.column_for(LogRole.MESSAGE))
        try:
            rc = run_root_cause(prov, lt.table, redacted)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"The model call failed: {exc}") from exc
        return rc.model_dump()

    @app.post("/api/logs/spikes")
    def post_log_spikes(body: SpikesBody) -> dict:
        # Deterministic time-trend: only aggregate counts leave the DB, so no provider gate needed.
        lt = choose_log_table(report.log_tables, body.table)
        if lt is None:
            raise HTTPException(status_code=404, detail="No matching log table in this map.")
        grain = (body.grain or "hour").lower().strip()
        if grain not in TIME_GRAINS:
            raise HTTPException(status_code=400,
                                detail=f"grain must be one of {', '.join(TIME_GRAINS)}.")
        since_hours = parse_since(body.since)
        bucket_sql = time_bucket_sql(lt, grain=grain, only_errors=body.only_errors,
                                     since_hours=since_hours)
        if bucket_sql is None:
            raise HTTPException(status_code=400,
                                detail="This log has no timestamp column to chart over time.")
        src_sql = source_time_bucket_sql(lt, grain=grain, only_errors=body.only_errors,
                                         since_hours=since_hours)
        try:
            with make_db() as db:
                bucket_rows = db.query(bucket_sql)
                source_rows = db.query(src_sql) if src_sql is not None else []
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Log query failed: {exc}") from exc
        report_obj = build_spike_report(lt, bucket_rows, source_rows, grain=grain,
                                        only_errors=body.only_errors)
        return report_obj.model_dump()

    @app.get("/")
    def index() -> HTMLResponse:
        """The app shell, with its asset URLs stamped so a stale bundle can never be reused."""
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__ASSET_VERSION__", _asset_version()),
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/static", _RevalidatingStatic(directory=_STATIC_DIR), name="static")
    return app
