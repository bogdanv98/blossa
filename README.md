# Blossa

**Blossa** connects to a (typically legacy, on-prem) **Oracle** database, reads it **read-only**, and
reconstructs the **business meaning** of its schema: what each table and column actually represents,
the likely relationships between them, and a human-readable **map of the whole database** — so a new
engineer can understand a database that nobody documented, without needing the person who built it.

The engine is **domain-agnostic**. The first wedge is **legacy Oracle on-prem**.

> Status: early MVP. CLI only. Produces a Markdown "database map" + a machine-readable JSON artifact.

## Why

When the people who understood a database leave, the knowledge of what the data *means* in business
terms walks out the door with them. Blossa accelerates understanding (gets a new person to ~70% in a
day instead of weeks). It is honest about its limits: it reconstructs what is *inferable* from schema
+ data + existing docs — it does not recover purely tacit knowledge that was never written down.

## How it works

```
Oracle (read-only)
   │  introspect data dictionary (ALL_TABLES, ALL_TAB_COLUMNS, ALL_CONSTRAINTS, …)
   ▼
[ deterministic core ]  ── PKs/FKs, candidate FKs, orphans, type/naming issues, missing comments
   │  build compact, PII-SAFE per-table summaries (aggregates + masked samples, never raw rows)
   ▼
[ LLM semantic pass ]   ── runs ONLY over the structured summaries (local model by default)
   │  → table purpose + column meaning, each with confidence + evidence
   ▼
[ renderer ]            ── database_map.md  +  database_map.json
```

The deterministic core does the heavy lifting. The LLM is used sparingly, only over compact
structured summaries — **never** over raw schema dumps or raw data.

## Privacy / safety (hard constraints)

- The database connection is **read-only**.
- **Never** sends raw row values to any LLM — only aggregates, value patterns, and masked samples.
- Runs **locally by default** (Ollama / vLLM). With a local model, Blossa makes **no external network calls**.
- Do **not** develop or test against production / employer data — use the bundled synthetic schema.

## Install

Once released to PyPI:

```bash
python -m pip install blossa
```

From source (for development):

```bash
python -m pip install -e ".[dev]"
```

Requires Python 3.12+. Uses the `oracledb` driver in **thin mode** — no Oracle Instant Client needed.

## First run (the fast path)

```bash
blossa init      # interactive: writes blossa.local.yml (DSN, user, schema, LLM choice)
blossa doctor    # checks Python, driver, config, Oracle, the LLM, and output dir — tells you what's missing
blossa scan      # run it
```

`blossa doctor` is the friend that tells a brand-new user exactly what to fix before scanning
(e.g. "Ollama not reachable — run `ollama pull qwen2.5:14b`", or "start the demo DB").

## Quick start

### 1. Bring up a synthetic Oracle (for development)

```bash
cd docker
docker compose up -d        # Oracle XE + the synthetic BLOSSA_DEMO schema
```

The seed script creates a few related tables with deliberately missing comments and one
**undeclared** foreign key, so the whole pipeline can be exercised without real data.

### 2. Configure

Copy `blossa.yml` → `blossa.local.yml` and set your DSN / credentials (or use `BLOSSA_*` env vars).
Prefer the env var for the password:

```bash
export BLOSSA_ORACLE__PASSWORD=blossa_demo
```

### 3. Scan

```bash
blossa scan --config blossa.local.yml
```

Outputs `out/database_map.md` and `out/database_map.json`.

### Try it without Oracle or a GPU

```bash
# Runs the full pipeline over a bundled offline fixture, with the heuristic (no-LLM) provider.
blossa scan --demo --llm-provider heuristic
```

## Commands

| Command | What it does |
| --- | --- |
| `blossa init` | Interactive first-run setup; writes `blossa.local.yml`. |
| `blossa doctor` | Check every prerequisite (Python, driver, config, Oracle, LLM, output) and report fixes. |
| `blossa scan` | Full pipeline against the configured Oracle schema → Markdown + JSON. |
| `blossa scan --demo` | Run against the bundled offline fixture (no Oracle needed). |
| `blossa introspect` | Just dump the raw introspected schema as JSON (no checks, no LLM). |
| `blossa check-llm` | Verify the configured LLM provider is reachable. |

Run `blossa --help` for all flags.

## Scope (MVP)

In: read-only Oracle introspection, deterministic schema analysis, PII-safe summaries, a local-LLM
semantic pass, Markdown + JSON output.

Out (for now): web UI, chat interface, any write access, non-Oracle engines, query-log/lineage
ingestion, managed cloud, model fine-tuning.

## Develop & release

```bash
python -m pip install -e ".[dev]"
ruff check src tests        # lint
pytest                      # tests
python -m build             # build sdist + wheel into dist/
```

CI (lint + tests on Python 3.12/3.13, Linux + Windows) runs via `.github/workflows/ci.yml`.

To publish to PyPI: configure a [Trusted Publisher](https://docs.pypi.org/trusted-publishers/)
for the project on PyPI, then push a version tag:

```bash
git tag v0.1.0 && git push --tags
```

`.github/workflows/release.yml` builds and publishes automatically (no API token needed).
Note: confirm the `blossa` name is available on PyPI before the first release.

## License

Blossa is licensed under the **[GNU Affero General Public License v3.0](LICENSE)** (AGPL-3.0-only).

In short: you can use, run, study, and modify Blossa freely — but if you distribute it or run a
modified version as a network service, you must make your source changes available under the same
license. See [NOTICE](NOTICE) for copyright.

Copyright (c) 2026 Bogdan Voinea. "Blossa" is the project name; the license covers the code, not the
name. A separate commercial license may be offered in the future — contributions are accepted under
the [CLA](CLA.md) (see [CONTRIBUTING.md](CONTRIBUTING.md)) to keep that option open.
