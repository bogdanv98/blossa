# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Blossa command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Settings, load_settings
from .db.connection import Database
from .db.introspect import introspect_schema
from .diagnostics import Status, run_diagnostics
from .diagnostics import check_llm as diag_check_llm
from .evaluation import build_ground_truth, evaluate
from .llm import get_provider
from .models import ScanReport
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
    """Interactive first-run setup - writes a blossa.local.yml you can scan with."""
    if output.exists() and not force:
        err.print(f"[yellow]{output} already exists.[/yellow] Re-run with --force to overwrite.")
        raise typer.Exit(code=1)

    console.print("[bold]Blossa setup[/bold] - answer a few questions (Enter accepts default).\n")

    console.print("[bold]1) Oracle connection (read-only)[/bold]")
    dsn = typer.prompt("  DSN (host:port/service)", default="localhost:1521/XEPDB1")
    user = typer.prompt("  Username", default="blossa_demo")
    schema = typer.prompt(
        "  Schema/owner to scan (blank = same as user)", default="", show_default=False
    )
    store_pw = typer.confirm("  Store the password in the file? (not recommended)", default=False)
    password = ""
    if store_pw:
        password = typer.prompt("  Password", hide_input=True, default="", show_default=False)

    console.print("\n[bold]2) LLM provider for the semantic pass[/bold]")
    console.print("  [dim]ollama = local model (private) | heuristic = offline, no model | "
                  "openai_compatible = remote endpoint[/dim]")
    provider = typer.prompt("  Provider [ollama/heuristic/openai_compatible]", default="ollama")
    ollama_model = "qwen2.5:14b"
    if provider == "ollama":
        ollama_model = typer.prompt("  Ollama model", default="qwen2.5:14b")

    data: dict = {
        "oracle": {"dsn": dsn, "user": user},
        "llm": {"provider": provider},
    }
    if schema.strip():
        data["oracle"]["schema"] = schema.strip()
    if password:
        data["oracle"]["password"] = password
    if provider == "ollama":
        data["llm"]["ollama"] = {"model": ollama_model}

    output.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    console.print(f"\n[bold green]Wrote[/bold green] {output}")
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
