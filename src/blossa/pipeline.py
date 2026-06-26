# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""End-to-end orchestration shared by the CLI.

    introspect -> profile -> deterministic checks -> PII-safe summaries -> semantic pass -> report

The CLI is a thin shell over these functions; keeping them here makes the pipeline testable
without a terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from . import __version__
from .checks import run_checks
from .config import Settings
from .db.connection import Database, QueryExecutor
from .db.introspect import introspect_schemas, list_non_system_schemas
from .db.profile import profile_table
from .llm import get_provider
from .llm.base import LLMProvider
from .logs import detect_log_tables
from .models import (
    Finding,
    LogTable,
    ProgramSemantics,
    Relationship,
    ScanMetadata,
    ScanReport,
    SchemaInfo,
    TableSemantics,
)
from .program import run_program_pass
from .semantic import run_semantic_pass
from .summary import build_summaries

StatusFn = Callable[[str], None]


def _noop(_msg: str) -> None:  # pragma: no cover - trivial
    pass


def _owners_to_scan(db: Database, settings: Settings) -> list[str]:
    """Which schemas to scan: explicit list, every non-system one ("*"), or the login user."""
    cfg = settings.oracle
    if cfg.scan_all_non_system:
        return list_non_system_schemas(db)
    explicit = cfg.explicit_owners()
    return explicit or [db.effective_schema]


def introspect_and_profile(
    db: Database, settings: Settings, status: StatusFn = _noop
) -> SchemaInfo:
    """Read the data dictionary and (unless disabled) attach PII-safe profiles."""
    owners = _owners_to_scan(db, settings)
    label = owners[0] if len(owners) == 1 else f"{len(owners)} schemas ({', '.join(owners)})"
    status(f"Introspecting {label} ...")
    schema = introspect_schemas(db, owners, use_dba=settings.oracle.use_dba_catalog)

    if settings.scan.max_tables and len(schema.tables) > settings.scan.max_tables:
        schema.tables = schema.tables[: settings.scan.max_tables]

    if not settings.scan.skip_profiling:
        for table in schema.tables:
            status(f"Profiling {table.name} ...")
            profile_table(db, table.owner or db.effective_schema, table, settings.scan.sample_rows)
    return schema


def analyze(
    schema: SchemaInfo,
    settings: Settings,
    db: QueryExecutor | None,
    owner: str | None,
) -> tuple[list[Relationship], list[Finding]]:
    return run_checks(
        schema,
        db=db,
        owner=owner,
        overlap_threshold=settings.scan.candidate_fk_overlap,
    )


def semantic(
    schema: SchemaInfo,
    relationships: list[Relationship],
    provider: LLMProvider,
    status: StatusFn = _noop,
) -> list[TableSemantics]:
    summaries = build_summaries(schema, relationships)

    def progress(table_name: str, i: int, total: int) -> None:
        status(f"Inferring meaning [{i}/{total}]: {table_name} ...")

    return run_semantic_pass(provider, summaries, progress=progress)


def program_semantics(
    schema: SchemaInfo,
    provider: LLMProvider,
    status: StatusFn = _noop,
) -> list[ProgramSemantics]:
    """Explain stored program units with the LLM. Needs a model provider; the offline heuristic
    can't read code, so we skip it (the units are still captured and shown, just unexplained)."""
    units = schema.program_units
    if not units or provider.name == "heuristic":
        return []
    known_tables = [t.name for t in schema.tables]

    def progress(unit_name: str, i: int, total: int) -> None:
        status(f"Explaining program logic [{i}/{total}]: {unit_name} ...")

    return run_program_pass(provider, units, known_tables, progress=progress)


def assemble_report(
    schema: SchemaInfo,
    relationships: list[Relationship],
    findings: list[Finding],
    semantics: list[TableSemantics],
    provider: LLMProvider,
    profiling_enabled: bool,
    programs: list[ProgramSemantics] | None = None,
    log_tables: list[LogTable] | None = None,
) -> ScanReport:
    metadata = ScanMetadata(
        blossa_version=__version__,
        schema_name=schema.name,
        generated_at=datetime.now(UTC),
        llm_provider=provider.name,
        llm_model=provider.model,
        profiling_enabled=profiling_enabled,
        table_count=len(schema.tables),
    )
    return ScanReport(
        metadata=metadata,
        schema_info=schema,
        relationships=relationships,
        findings=findings,
        semantics=semantics,
        program_semantics=programs or [],
        log_tables=log_tables or [],
    )


def run_scan_over_schema(
    schema: SchemaInfo,
    settings: Settings,
    provider: LLMProvider,
    db: QueryExecutor | None,
    owner: str | None,
    status: StatusFn = _noop,
) -> ScanReport:
    """Run checks → summaries → semantic pass → report over an already-introspected schema."""
    status("Running deterministic checks ...")
    relationships, findings = analyze(schema, settings, db, owner)
    log_tables = detect_log_tables(schema)
    if log_tables:
        status(f"Recognised {len(log_tables)} application log table(s).")
    semantics = semantic(schema, relationships, provider, status)
    programs = program_semantics(schema, provider, status)
    return assemble_report(
        schema,
        relationships,
        findings,
        semantics,
        provider,
        profiling_enabled=not settings.scan.skip_profiling,
        programs=programs,
        log_tables=log_tables,
    )


def scan_oracle(settings: Settings, status: StatusFn = _noop) -> ScanReport:
    """Full live scan against the configured Oracle schema."""
    provider = get_provider(settings.llm)
    with Database(settings.oracle) as db:
        schema = introspect_and_profile(db, settings, status)
        return run_scan_over_schema(
            schema, settings, provider, db=db, owner=db.effective_schema, status=status
        )
