# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Blossa command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .config import Settings, load_settings
from .db.connection import Database
from .db.introspect import introspect_schema
from .diagnostics import Status, run_diagnostics
from .diagnostics import check_llm as diag_check_llm
from .evaluation import build_ground_truth, evaluate
from .grants import build_grants_sql
from .llm import get_provider
from .logsense import (
    LOCAL_PROVIDERS,
    TIME_GRAINS,
    bucket_entries_sql,
    build_spike_report,
    choose_log_table,
    local_only_message,
    parse_since,
    recent_entries_sql,
    redact_entries,
    run_root_cause,
    severity_breakdown_sql,
    source_breakdown_sql,
    source_time_bucket_sql,
    time_bucket_sql,
)
from .models import LogRole, ScanReport
from .nlquery import (
    ASK_SYSTEM_PROMPT,
    AskResult,
    Turn,
    UnsafeQueryError,
    build_ask_prompt,
    enforce_error_severity_filter,
    parse_ask_response,
    privilege_hint,
    validate_read_only_select,
    with_row_limit,
)
from .pipeline import run_scan_over_schema, scan_oracle
from .render import write_json, write_markdown

_STATUS_STYLE = {
    Status.OK: "[bold green]OK[/bold green]",
    Status.WARN: "[bold yellow]WARN[/bold yellow]",
    Status.FAIL: "[bold red]FAIL[/bold red]",
}

app = typer.Typer(
    name="blossa",
    help="Reconstruct the business meaning of a legacy Oracle schema (read-only, local-first).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


def _status(msg: str) -> None:
    console.print(f"[dim]>[/dim] {msg}")


def _apply_overrides(
    settings: Settings,
    llm_provider: str | None,
    skip_profiling: bool | None,
    output_dir: str | None,
) -> Settings:
    if llm_provider:
        settings.llm.provider = llm_provider
    if skip_profiling is not None:
        settings.scan.skip_profiling = skip_profiling
    if output_dir:
        settings.output.dir = output_dir
    return settings


@app.command()
def version() -> None:
    """Print the Blossa version."""
    console.print(f"Blossa {__version__}")


@app.command()
def scan(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to a blossa.yml config file."
    ),
    demo: bool = typer.Option(
        False, "--demo", help="Run over the bundled synthetic schema (no Oracle needed)."
    ),
    llm_provider: str | None = typer.Option(
        None, "--llm-provider", help="Override provider: ollama | openai_compatible | heuristic."
    ),
    skip_profiling: bool = typer.Option(
        False, "--skip-profiling", help="Skip data profiling (structure + comments only)."
    ),
    output_dir: str | None = typer.Option(None, "--out", help="Output directory."),
) -> None:
    """Scan a schema end-to-end and write a Markdown database map + JSON artifact."""
    settings = load_settings(config)
    settings = _apply_overrides(
        settings, llm_provider, skip_profiling if skip_profiling else None, output_dir
    )

    try:
        if demo:
            report = _scan_demo(settings)
        else:
            _preflight_llm(settings)
            report = scan_oracle(settings, status=_status)
    except Exception as exc:  # noqa: BLE001 - surface a clean message, not a traceback
        err.print(f"[bold red]Scan failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    out_base = Path(settings.output.dir) / settings.output.name
    md_path = write_markdown(report, out_base.with_suffix(".md"))
    json_path = write_json(report, out_base.with_suffix(".json"))

    _print_summary(report, md_path, json_path)


def _scan_demo(settings: Settings):
    from .demo import build_demo_schema

    if settings.llm.provider == "ollama":
        # The demo defaults to the offline provider so it never needs a running model.
        _status("Demo mode: using the offline 'heuristic' provider (override with --llm-provider).")
        settings.llm.provider = "heuristic"

    provider = get_provider(settings.llm)
    schema = build_demo_schema()
    _status(f"Loaded demo schema {schema.name} ({len(schema.tables)} tables).")
    # No live DB on the demo path → name-based candidate FKs only.
    return run_scan_over_schema(schema, settings, provider, db=None, owner=None, status=_status)


def _preflight_llm(settings: Settings) -> None:
    """Fail fast with actionable guidance if the chosen LLM provider isn't usable."""
    result = diag_check_llm(settings)
    if result.status == Status.OK:
        return
    err.print(f"[bold red]LLM not ready:[/bold red] {result.detail}")
    if result.hint:
        for line in result.hint.splitlines():
            err.print(f"  {line.strip()}")
    err.print(
        "  [dim]Tip: run `blossa doctor` to check everything, or "
        "`blossa scan --llm-provider heuristic` to run fully offline.[/dim]"
    )
    raise typer.Exit(code=1)


def _print_summary(report, md_path: Path, json_path: Path) -> None:
    m = report.metadata
    console.print()
    console.print(f"[bold green]Scan complete[/bold green] - schema [bold]{m.schema_name}[/bold]")
    console.print(f"  tables: {m.table_count}  relationships: {len(report.relationships)}  "
                  f"findings: {len(report.findings)}")
    console.print(f"  provider: {m.llm_provider}" + (f" ({m.llm_model})" if m.llm_model else ""))
    console.print(f"  [cyan]{md_path}[/cyan]")
    console.print(f"  [cyan]{json_path}[/cyan]")


def _load_map_report(settings: Settings, map_path: Path | None) -> ScanReport:
    """Load the scan JSON map to ground on, or exit with actionable guidance if it's missing."""
    if map_path is None:
        map_path = Path(settings.output.dir) / f"{settings.output.name}.json"
    if not map_path.exists():
        err.print(
            f"[bold red]No database map at[/bold red] {map_path}. "
            "Run [cyan]blossa scan[/cyan] first, or point to one with [cyan]--map[/cyan]."
        )
        raise typer.Exit(code=1)
    try:
        return ScanReport.model_validate_json(map_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Could not read the map:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _require_ask_provider(settings: Settings):
    """Ensure a usable model provider for `ask` (the offline heuristic can't translate to SQL)."""
    if settings.llm.provider == "heuristic":
        err.print(
            "[bold red]`ask` needs a model provider[/bold red] (ollama or openai_compatible); "
            "the offline heuristic can't translate questions to SQL."
        )
        raise typer.Exit(code=1)
    _preflight_llm(settings)
    return get_provider(settings.llm)


def _print_proposal(result: AskResult) -> None:
    console.print()
    console.print(Syntax(result.sql, "sql", theme="ansi_dark", word_wrap=True))
    if result.explanation:
        console.print(f"[dim]{result.explanation}[/dim]")
    if result.assumptions:
        console.print("[bold]Assumptions:[/bold]")
        for a in result.assumptions:
            console.print(f"  - {a}")
    console.print(f"[dim]Confidence:[/dim] {result.confidence.value}")


def _answer_ask_turn(
    question: str,
    report: ScanReport,
    provider,
    settings: Settings,
    *,
    history: list[Turn],
    max_rows: int,
    dry_run: bool,
    strict: bool,
) -> AskResult | None:
    """Translate one question to SQL (optionally as a follow-up) and run it.

    `history` carries prior question+SQL pairs so a follow-up can refine the last query; only that
    metadata reaches the model, never results. `strict` (one-shot CLI) turns failures into process
    exits with meaningful codes; interactive mode passes strict=False so the session continues.
    Returns the proposal (whatever the model produced), or None if the model call itself failed.
    """
    _status("Translating your question to SQL ...")
    try:
        prompt = build_ask_prompt(
            question, report, use_dba=settings.oracle.use_dba_catalog, history=history
        )
        raw = provider.generate(ASK_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]The model call failed:[/bold red] {exc}")
        if strict:
            raise typer.Exit(code=1) from exc
        return None
    result = parse_ask_response(raw)
    result = enforce_error_severity_filter(question, result, report)

    if not result.answerable:
        # No SQL: either a plain-language answer (e.g. "what does this procedure do") drawn from
        # the map's program summaries, or a genuine "can't answer". Either way the model's text is
        # the response — show it.
        if result.explanation:
            console.print(result.explanation)
            return result
        err.print("[yellow]I couldn't turn that into a query for this schema.[/yellow]")
        if strict:
            raise typer.Exit(code=2)
        return result

    _print_proposal(result)

    if dry_run:
        console.print("[dim](--dry-run: query not executed)[/dim]")
        return result

    try:
        safe_sql = validate_read_only_select(result.sql)
    except UnsafeQueryError as exc:
        err.print(f"[bold red]Refusing to run this query:[/bold red] {exc}")
        if strict:
            raise typer.Exit(code=1) from exc
        return result

    try:
        with Database(settings.oracle) as db:
            rows = db.query(with_row_limit(safe_sql, max_rows))
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Query failed:[/bold red] {exc}")
        hint = privilege_hint(safe_sql, str(exc))
        if hint:
            err.print(f"  [dim]{hint}[/dim]")
        if strict:
            raise typer.Exit(code=1) from exc
        return result

    _print_rows(rows, max_rows)
    return result


def _interactive_ask(
    report: ScanReport, provider, settings: Settings, max_rows: int, dry_run: bool
) -> None:
    """A multi-turn REPL: ask a question, then refine it; each turn can build on the last query."""
    console.print(
        "[bold]Interactive ask[/bold] - ask a question, then refine it "
        "(e.g. [italic]now break it down by year[/italic], [italic]only the top 5[/italic]).\n"
        "[dim]Blank line or [cyan]exit[/cyan] to quit; [cyan]reset[/cyan] to start a fresh "
        "thread.[/dim]\n"
    )
    history: list[Turn] = []
    while True:
        try:
            question = typer.prompt(
                "ask", default="", show_default=False, prompt_suffix="> "
            ).strip()
        except (EOFError, typer.Abort, KeyboardInterrupt):
            break
        if not question or question.lower() in {"exit", "quit"}:
            break
        if question.lower() == "reset":
            history = []
            console.print("[dim]Started a fresh thread.[/dim]\n")
            continue
        result = _answer_ask_turn(
            question, report, provider, settings,
            history=history, max_rows=max_rows, dry_run=dry_run, strict=False,
        )
        if result is not None:
            # Record the turn so the next question can refine it. Only the question and the model's
            # own SQL are kept — never the rows.
            history.append(Turn(question=question, sql=result.sql))
        console.print()
    console.print("[dim]Bye.[/dim]")


@app.command()
def ask(
    question: str | None = typer.Argument(
        None, help="Your question, in plain language. Omit it to start an interactive session."
    ),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a config file."),
    map_path: Path | None = typer.Option(
        None, "--map", "-m", help="Scan JSON map to ground on (default: <out>/<name>.json)."
    ),
    llm_provider: str | None = typer.Option(None, "--llm-provider"),
    max_rows: int = typer.Option(100, "--max-rows", help="Cap the number of rows returned."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the SQL but do not run it."),
) -> None:
    """Answer a natural-language question with a read-only SQL query over the scanned schema.

    Grounds on the database map from `blossa scan`. The model sees only the semantic map (never
    raw data); the generated SQL is shown to you and validated read-only before it runs.

    Pass a question for a one-shot answer, or omit it to open an interactive session where each
    follow-up ("now break it down by year") refines the previous query.
    """
    settings = load_settings(config)
    if llm_provider:
        settings.llm.provider = llm_provider

    report = _load_map_report(settings, map_path)
    provider = _require_ask_provider(settings)

    if question is not None and question.strip():
        _answer_ask_turn(
            question.strip(), report, provider, settings,
            history=[], max_rows=max_rows, dry_run=dry_run, strict=True,
        )
        return
    _interactive_ask(report, provider, settings, max_rows, dry_run)


def _print_rows(rows: list[dict], max_rows: int) -> None:
    console.print()
    if not rows:
        console.print("[dim]No rows returned.[/dim]")
        return
    table = Table(show_lines=False)
    for col in rows[0]:
        table.add_column(str(col), overflow="fold")
    for r in rows:
        table.add_row(*["" if v is None else str(v) for v in r.values()])
    console.print(table)
    note = f"{len(rows)} row(s)"
    if len(rows) >= max_rows:
        note += f", capped at --max-rows={max_rows}"
    console.print(f"[dim]{note}. Results are shown to you only - not sent to the model.[/dim]")


@app.command()
def serve(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a config file."),
    map_path: Path | None = typer.Option(
        None, "--map", "-m", help="Scan JSON map to serve (default: <out>/<name>.json)."
    ),
    llm_provider: str | None = typer.Option(None, "--llm-provider"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (localhost by default)."),
    port: int = typer.Option(8000, "--port", help="Port to listen on."),
) -> None:
    r"""Serve a local web UI: browse the database map and ask questions in plain language.

    Binds to localhost only by default. Needs the web extra: `pip install blossa\[web]`.
    """
    settings = load_settings(config)
    if llm_provider:
        settings.llm.provider = llm_provider

    report = _load_map_report(settings, map_path)

    try:
        import uvicorn

        from .web.server import create_app
    except ImportError:
        err.print(
            "[bold red]The web UI needs the 'web' extra.[/bold red] "
            r"Install it with: [cyan]pip install blossa\[web][/cyan]"
        )
        raise typer.Exit(code=1) from None

    console.print(
        f"[bold green]Blossa[/bold green] serving [bold]{report.metadata.schema_name}[/bold] "
        f"at [cyan]http://{host}:{port}[/cyan]  [dim](Ctrl+C to stop)[/dim]"
    )
    uvicorn.run(create_app(settings, report), host=host, port=port, log_level="warning")


def _print_simple_table(title: str, rows: list[dict]) -> None:
    console.print(f"\n[bold]{title}[/bold]")
    if not rows:
        console.print("[dim]  (none)[/dim]")
        return
    table = Table(show_lines=False)
    for col in rows[0]:
        table.add_column(str(col), overflow="fold")
    for r in rows:
        table.add_row(*["" if v is None else str(v) for v in r.values()])
    console.print(table)


def _print_spike_report(report) -> None:  # noqa: ANN001 - SpikeReport, kept loose to avoid import
    """ASCII-only render of the time trend + flagged spikes (Windows console safe)."""
    window = "errors only" if report.only_errors else "all entries"
    console.print(f"\n[bold]Volume by {report.grain}[/bold] [dim]({window}; baseline "
                  f"{report.baseline:g}/bucket, spike = >={report.factor:g}x and "
                  f">={report.min_count})[/dim]")
    if not report.buckets:
        console.print(f"[dim]  {report.note}[/dim]")
        return

    peak = max((b.count for b in report.buckets), default=1) or 1
    spike_buckets = {s.bucket for s in report.spikes}
    table = Table(show_lines=False)
    table.add_column("bucket")
    table.add_column("count", justify="right")
    table.add_column("")
    table.add_column("")
    for b in report.buckets:
        bar = "#" * max(1, round(20 * b.count / peak)) if b.count else ""
        flag = "[bold red]SPIKE[/bold red]" if b.bucket in spike_buckets else ""
        style = "bold red" if b.bucket in spike_buckets else None
        table.add_row(b.bucket, str(b.count), bar, flag, style=style)
    console.print(table)

    if report.spikes:
        console.print(f"\n[bold red]{len(report.spikes)} spike(s) detected[/bold red] "
                      f"[dim](peak {max(s.count for s in report.spikes)} vs baseline "
                      f"{report.baseline:g})[/dim]")
    else:
        console.print(f"[dim]{report.note}[/dim]")

    if report.onsets:
        console.print("\n[bold]Sources that started spiking[/bold]")
        for o in report.onsets:
            name = o.source or "(unattributed)"
            console.print(f"  {name}  [dim]at {o.bucket} ({o.count}, {o.ratio:g}x baseline)[/dim]")


def _explain_entries(provider, lt, rows: list[dict], *, header: str) -> None:  # noqa: ANN001
    """Redact, cluster into root causes via the local model, and print them (ASCII only)."""
    if not rows:
        console.print("\n[dim]No matching error entries to explain.[/dim]")
        return
    msg_col = lt.column_for(LogRole.MESSAGE)
    redacted = redact_entries(rows, msg_col)
    try:
        rc = run_root_cause(provider, lt.table, redacted)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]The model call failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"\n[bold]{header}[/bold] [dim](from {rc.sample_size} redacted entries; "
                  "text stayed on this machine)[/dim]")
    if not rc.clusters:
        console.print(f"[dim]{rc.note}[/dim]")
        return
    for c in rc.clusters:
        head = f"[bold]{c.cause}[/bold]"
        if c.count:
            head += f"  [dim]x{c.count}[/dim]"
        if c.severity:
            head += f"  [dim]{c.severity}[/dim]"
        console.print(f"\n{head}")
        if c.suggested_action:
            console.print(f"  -> {c.suggested_action}")
        if c.example:
            console.print(f"  [dim]e.g. {c.example}[/dim]")


@app.command()
def logs(
    table: str | None = typer.Argument(
        None, help="Log table to analyse (default: auto-pick the main error log)."
    ),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a config file."),
    map_path: Path | None = typer.Option(
        None, "--map", "-m", help="Scan JSON map to use (default: <out>/<name>.json)."
    ),
    llm_provider: str | None = typer.Option(None, "--llm-provider"),
    limit: int = typer.Option(50, "--limit", help="How many recent entries to show / analyse."),
    all_severities: bool = typer.Option(
        False, "--all", help="Include non-error entries (default: errors/failures only)."
    ),
    spikes: bool = typer.Option(
        False, "--spikes", help="Show error volume over time and flag spikes (deterministic)."
    ),
    by: str = typer.Option(
        "hour", "--by", help="Time bucket for --spikes: hour or day."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Limit --spikes to a recent window, e.g. 48h or 7d."
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Cluster recent errors into root causes (LOCAL model only)."
    ),
) -> None:
    """Analyse an application log/error/audit table: breakdowns, recent entries, root causes.

    The breakdowns and recent entries are plain read-only SQL — results are shown to you only.
    `--spikes` charts error volume over time buckets and flags abnormal jumps (also deterministic —
    only aggregate counts leave the database). `--explain` additionally sends the (PII-redacted)
    error text to the model to cluster root causes; because that reads real row text, it is allowed
    only with a LOCAL model (e.g. Ollama), where nothing leaves your machine. Combine
    `--spikes --explain` to root-cause the biggest spike window specifically.
    """
    settings = load_settings(config)
    if llm_provider:
        settings.llm.provider = llm_provider
    report = _load_map_report(settings, map_path)

    if not report.log_tables:
        err.print(
            "[yellow]No application log tables were recognised in this map.[/yellow] "
            "Scan a schema that has error/audit/job-log tables first."
        )
        raise typer.Exit(code=1)

    lt = choose_log_table(report.log_tables, table)
    if lt is None:
        known = ", ".join(x.table for x in report.log_tables)
        err.print(f"[bold red]No log table named[/bold red] {table}. Known: {known}")
        raise typer.Exit(code=1)

    # Fail fast on the privacy gate before touching the database.
    provider = None
    if explain:
        if settings.llm.provider == "heuristic":
            err.print("[bold red]--explain needs a model provider[/bold red] (the offline "
                      "heuristic can't read error text).")
            raise typer.Exit(code=1)
        if settings.llm.provider not in LOCAL_PROVIDERS:
            err.print(f"[bold red]Refusing to send error text to a remote model.[/bold red]\n"
                      f"  [dim]{local_only_message()}[/dim]")
            raise typer.Exit(code=1)
        _preflight_llm(settings)
        provider = get_provider(settings.llm)

    only_errors = not all_severities
    console.print(f"[bold]{lt.table}[/bold] [dim]({lt.kind.value} log, {lt.confidence.value} "
                  f"confidence)[/dim]")

    if spikes:
        grain = by.lower().strip()
        if grain not in TIME_GRAINS:
            err.print(f"[bold red]--by must be one of {', '.join(TIME_GRAINS)}[/bold red].")
            raise typer.Exit(code=1)
        since_hours = parse_since(since)
        bucket_sql = time_bucket_sql(lt, grain=grain, only_errors=only_errors,
                                     since_hours=since_hours)
        if bucket_sql is None:
            err.print("[yellow]This log has no timestamp column, so it can't be charted over "
                      "time.[/yellow]")
            raise typer.Exit(code=1)
        src_sql = source_time_bucket_sql(lt, grain=grain, only_errors=only_errors,
                                         since_hours=since_hours)
        try:
            with Database(settings.oracle) as db:
                bucket_rows = db.query(bucket_sql)
                source_rows = db.query(src_sql) if src_sql is not None else []
        except Exception as exc:  # noqa: BLE001
            err.print(f"[bold red]Log query failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        report = build_spike_report(lt, bucket_rows, source_rows, grain=grain,
                                    only_errors=only_errors)
        _print_spike_report(report)
        console.print("[dim]Only aggregate counts left the database - no row text.[/dim]")

        if not explain or not report.spikes:
            return
        # --explain: root-cause the biggest spike window (local model only, gate already passed).
        top = max(report.spikes, key=lambda s: s.count)
        _status(f"Clustering errors in the spike at {top.bucket} (local model) ...")
        win_sql = bucket_entries_sql(lt, top.bucket, grain=grain, limit=limit,
                                     only_errors=only_errors)
        try:
            with Database(settings.oracle) as db:
                win_rows = db.query(win_sql)
        except Exception as exc:  # noqa: BLE001
            err.print(f"[bold red]Log query failed:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        _explain_entries(provider, lt, win_rows, header=f"Root causes in the {top.bucket} spike")
        return

    try:
        with Database(settings.oracle) as db:
            if (sev_sql := severity_breakdown_sql(lt)) is not None:
                _print_simple_table("By severity", db.query(sev_sql))
            if (src_sql := source_breakdown_sql(lt, only_errors=only_errors)) is not None:
                label = "Top sources (errors)" if only_errors else "Top sources"
                _print_simple_table(label, db.query(src_sql))
            recent_rows = db.query(recent_entries_sql(lt, limit=limit, only_errors=only_errors))
            _print_simple_table(
                "Most recent errors" if only_errors else "Most recent entries", recent_rows
            )
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Log query failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print("[dim]Results are shown to you only - not sent to the model.[/dim]")

    if not explain:
        return
    _status("Clustering recent errors into root causes (local model) ...")
    _explain_entries(provider, lt, recent_rows, header="Root causes")


@app.command()
def introspect(
    config: Path | None = typer.Option(None, "--config", "-c"),
    pretty: bool = typer.Option(True, "--pretty/--compact"),
) -> None:
    """Introspect the configured schema and print the raw structure as JSON (no checks, no LLM)."""
    settings = load_settings(config)
    try:
        with Database(settings.oracle) as db:
            schema = introspect_schema(db, db.effective_schema)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Introspection failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(schema.model_dump_json(indent=2 if pretty else None))


@app.command("check-llm")
def check_llm(
    config: Path | None = typer.Option(None, "--config", "-c"),
    llm_provider: str | None = typer.Option(None, "--llm-provider"),
) -> None:
    """Verify that the configured LLM provider is reachable."""
    settings = load_settings(config)
    if llm_provider:
        settings.llm.provider = llm_provider
    result = diag_check_llm(settings)
    console.print(f"{_STATUS_STYLE[result.status]} - {result.detail}")
    if result.hint and result.status != Status.OK:
        for line in result.hint.splitlines():
            console.print(f"  {line.strip()}")
    if result.status == Status.FAIL:
        raise typer.Exit(code=1)


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a config file."),
) -> None:
    """Check every prerequisite (Python, driver, config, Oracle, LLM, output) and report fixes."""
    settings = load_settings(config)
    diag = run_diagnostics(settings, config)

    table = Table(title="blossa doctor", show_lines=False, expand=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details", overflow="fold")
    for r in diag.results:
        details = r.detail
        if r.hint and r.status != Status.OK:
            details += f"\n[dim]{r.hint}[/dim]"
        table.add_row(r.name, _STATUS_STYLE[r.status], details)
    console.print(table)

    if diag.has_failures:
        err.print("[bold red]Some checks failed.[/bold red] Fix the items above, then re-run.")
        raise typer.Exit(code=1)
    console.print("[bold green]All required checks passed.[/bold green]")


@app.command()
def init(
    output: Path = typer.Option(
        Path("blossa.local.yml"), "--output", "-o", help="Where to write the config."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Interactive first-run setup - writes blossa.local.yml + a DBA grant script you can run."""
    if output.exists() and not force:
        err.print(f"[yellow]{output} already exists.[/yellow] Re-run with --force to overwrite.")
        raise typer.Exit(code=1)

    console.print("[bold]Blossa setup[/bold] - answer a few questions (Enter accepts default).\n")

    console.print("[bold]1) Oracle connection (read-only)[/bold]")
    dsn = typer.prompt("  DSN (host:port/service)", default="localhost:1521/XEPDB1")
    user = typer.prompt("  Account Blossa connects as", default="BLOSSA_ASSISTANT")

    console.print("\n[bold]2) Access profile[/bold]")
    console.print("  [dim]scoped = read only the schemas you list (safe default) | "
                  "full = read the whole database (needs SELECT_CATALOG_ROLE)[/dim]")
    profile = typer.prompt("  Profile [scoped/full]", default="scoped").strip().lower()
    schemas: list[str] = []
    schema_cfg: str | list[str] | None = None
    if profile == "full":
        raw_schema = typer.prompt(
            "  Schema(s) to scan (blank = all non-system)", default="", show_default=False
        )
        schema_cfg = raw_schema.strip() or "*"
    else:
        profile = "scoped"
        raw_schemas = typer.prompt("  Schemas Blossa may read (comma-separated, e.g. HR, SALES)")
        schemas = [s.strip().upper() for s in raw_schemas.split(",") if s.strip()]
        schema_cfg = schemas[0] if len(schemas) == 1 else schemas

    store_pw = typer.confirm("  Store the password in the file? (not recommended)", default=False)
    password = ""
    if store_pw:
        password = typer.prompt("  Password", hide_input=True, default="", show_default=False)

    console.print("\n[bold]3) LLM provider for the semantic pass[/bold]")
    console.print("  [dim]ollama = local model (private) | heuristic = offline, no model | "
                  "openai_compatible = remote endpoint[/dim]")
    provider = typer.prompt("  Provider [ollama/heuristic/openai_compatible]", default="ollama")
    ollama_model = "qwen2.5:14b"
    if provider == "ollama":
        ollama_model = typer.prompt("  Ollama model", default="qwen2.5:14b")

    data: dict = {
        "oracle": {"dsn": dsn, "user": user, "catalog_scope": profile},
        "llm": {"provider": provider},
    }
    if schema_cfg:
        data["oracle"]["schema"] = schema_cfg
    if password:
        data["oracle"]["password"] = password
    if provider == "ollama":
        data["llm"]["ollama"] = {"model": ollama_model}

    output.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    console.print(f"\n[bold green]Wrote[/bold green] {output}")

    # Emit the grant script for a DBA to review and run (Blossa never runs it itself).
    grants_path = output.parent / "blossa_grants.sql"
    try:
        grants_sql = build_grants_sql(user, profile, schemas or None)
        grants_path.write_text(grants_sql, encoding="utf-8")
        console.print(f"[bold green]Wrote[/bold green] {grants_path}  [dim](grant script)[/dim]")
        console.print(
            f"  [dim]Hand {grants_path.name} to a DBA to review and run — it creates the "
            f"read-only account [bold]{user}[/bold]. Blossa never runs it itself.[/dim]"
        )
    except ValueError as exc:
        err.print(f"  [yellow]Skipped grant script:[/yellow] {exc}")

    if not password:
        console.print("  [dim]Set the password before scanning:[/dim] "
                      "export BLOSSA_ORACLE__PASSWORD=...")
    console.print("  Next: [cyan]blossa doctor[/cyan]  then  [cyan]blossa scan[/cyan]")


@app.command("ground-truth")
def ground_truth(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a config file."),
    output: Path = typer.Option(
        Path("ground_truth.json"), "--output", "-o", help="Where to write the ground-truth JSON."
    ),
) -> None:
    """Capture real comments + foreign keys from a documented schema (for later evaluation).

    Run this BEFORE stripping docs / dropping FKs, so you have a baseline to evaluate against.
    """
    settings = load_settings(config)
    try:
        with Database(settings.oracle) as db:
            schema = introspect_schema(db, db.effective_schema)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Ground-truth capture failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    truth = build_ground_truth(schema)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    n_tables = len(truth["tables"])
    n_fks = sum(len(t["foreign_keys"]) for t in truth["tables"].values())
    console.print(f"[bold green]Wrote[/bold green] {output} "
                  f"({n_tables} tables, {n_fks} foreign keys captured).")


@app.command("eval")
def eval_cmd(
    truth: Path = typer.Option(..., "--truth", "-t", help="Ground-truth JSON from `ground-truth`."),
    scan_json: Path = typer.Option(
        Path("out/database_map.json"), "--scan", "-s", help="The scan's JSON artifact."
    ),
) -> None:
    """Score a scan against ground truth: FK rediscovery + documentation coverage."""
    try:
        truth_data = json.loads(truth.read_text(encoding="utf-8"))
        report = ScanReport.model_validate_json(scan_json.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        err.print(f"[bold red]Could not load inputs:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    result = evaluate(truth_data, report)

    table = Table(title=f"blossa eval - {result.schema_name}")
    table.add_column("Metric", style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Detail")
    table.add_row(
        "FK rediscovery (recall)",
        f"{result.fk.recall:.0%}",
        f"{result.fk.matched}/{result.fk.truth_total} real FKs re-found",
    )
    table.add_row(
        "FK precision",
        f"{result.fk.precision:.0%}",
        f"{result.fk.matched}/{result.fk.found_total} inferred are real",
    )
    td, cd = result.table_docs, result.column_docs
    table.add_row(
        "Table doc coverage",
        f"{td.coverage:.0%}",
        f"{td.recovered}/{td.documented} documented tables got a meaning",
    )
    table.add_row(
        "Column doc coverage",
        f"{cd.coverage:.0%}",
        f"{cd.recovered}/{cd.documented} documented columns got a meaning",
    )
    console.print(table)
    if result.fk.missed:
        console.print("[dim]Real FKs not rediscovered:[/dim]")
        for m in result.fk.missed:
            console.print(f"  - {m}")


def main() -> None:  # pragma: no cover - entry shim
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
