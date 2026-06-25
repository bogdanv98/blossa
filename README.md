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
[ deterministic core ]  ── PKs/FKs, candidate FKs (self / composite / cross-schema), orphans, type/naming issues, missing comments
   │  build compact, PII-SAFE per-table summaries (aggregates + masked samples, never raw rows)
   ▼
[ LLM semantic pass ]   ── runs ONLY over the structured summaries (local model by default)
   │  → table purpose + column meaning, each with confidence + evidence
   ▼
[ renderer ]            ── database_map.md  +  database_map.json
```

The deterministic core does the heavy lifting. The LLM is used sparingly, only over compact
structured summaries — **never** over raw schema dumps or raw data.

## Relationship & multi-schema inference

Legacy schemas have usually lost their foreign-key declarations, so Blossa re-infers relationships
from column names **plus actual data overlap**, not only from declared constraints:

- **Exact-name candidates** — a column named like a key elsewhere (`ORDERS.CUST_ID → CUSTOMERS`),
  confirmed by value overlap.
- **Self-referential / role-named keys** — `EMPLOYEES.MANAGER_ID → EMPLOYEES.EMPLOYEE_ID`, matched
  by name suffix + type and disambiguated by the data.
- **Composite (multi-column) keys** — a child carrying every column of a multi-column key, by exact
  name **or** by suffix / role name, confirmed by tuple overlap.
- **Cross-schema keys** — when a scan spans several schemas, foreign keys that point *into another
  schema* become discoverable (a single-schema scan never sees the parent).

A scan can target one schema, a list, or `"*"` (every application schema, auto-discovered via
`ALL_USERS.ORACLE_MAINTAINED`). With several schemas in scope the database map is grouped per owner
and cross-schema links are labelled. Data-backed inference needs a live connection; offline, Blossa
falls back to conservative name-only candidates. See **[samples/README.md](samples/README.md)** for
worked, measured examples of each.

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
| `blossa ask "<question>"` | Ask a plain-language question; grounds on the map, shows the SQL, runs it read-only. |
| `blossa serve` | Local web UI: browse the map + ask questions in a browser (needs `blossa[web]`). |
| `blossa introspect` | Just dump the raw introspected schema as JSON (no checks, no LLM). |
| `blossa check-llm` | Verify the configured LLM provider is reachable. |
| `blossa ground-truth` | Capture real comments + FKs from a documented schema (for evaluation). |
| `blossa eval` | Score a scan against ground truth: FK rediscovery + documentation coverage. |

Run `blossa --help` for all flags.

## Ask questions in plain language

Once you have a map, a business user can query the database **without writing SQL or asking the DBA**:

```bash
blossa ask "How many employees are in each department?" --llm-provider ollama
```

Blossa grounds the model on the **semantic map** (table/column meanings + relationships — never raw
rows), turns the question into **one read-only Oracle SELECT**, and always shows you that SQL plus
the assumptions it made and a confidence level, so the answer can be verified rather than trusted
blindly. The query is validated to be a single read-only SELECT before it runs (and the connection
is a READ ONLY transaction regardless). Results are shown to you only — they are not sent back to the
model. Use `--dry-run` to see the SQL without executing it, and `--max-rows` to cap the output.

`ask` also answers questions about the **database itself** (how many schemas, which tables exist,
row counts, columns, constraints) from Oracle's data dictionary — see _Access_ below for how the
`scoped` vs `full` profile decides whether that covers just your schemas or the whole database.

It can also explain the **application logic**: ask _"what does the SECURE_DML procedure do?"_ and
Blossa answers in plain language (no SQL to run). This works because the scan reads the **source**
of stored procedures, functions, packages, triggers and views, and has the model summarise what
each one does and which tables it touches — captured in the map (and shown in the web UI's **Logic**
tab). Source code is DDL/metadata, not row data, so it stays within the same privacy boundary as
the table structure; raw rows are still never sent to the model. (Reading another schema's PL/SQL
needs the `full` profile, whose `SELECT_CATALOG_ROLE` exposes the source views.)

`ask` needs a model provider (Ollama / OpenAI-compatible) — the offline heuristic can't translate
language to SQL or read code.

## Access: a least-privilege read-only account

Blossa is designed to run as a dedicated, read-only account — **never** as SYSTEM/SYS. `blossa init`
asks how much you want it to see and **generates a `blossa_grants.sql` script for your DBA to review
and run** (Blossa never creates the account or grants itself). Two profiles:

- **scoped** (default, recommended) — `READ` on only the schemas you list. Oracle then limits both
  the data *and* the catalog questions (via the `ALL_*` views) to exactly those schemas — the scope
  is enforced by the database, not by Blossa. Easiest for a DBA to approve.
- **full** — `READ ANY TABLE` + `SELECT_CATALOG_ROLE`: read the whole database and answer catalog
  questions over the `DBA_*` views. Opt-in, for teams that want the complete picture.

The `oracle.catalog_scope: scoped | full` config flag (default `scoped`) tells `ask`/`serve` which
data-dictionary views to use. The connection always runs in a READ ONLY transaction regardless.

## Web UI (browse + ask in a browser)

For a non-technical analyst, the same thing is available as a small local web app:

```bash
pip install "blossa[web]"
blossa serve --llm-provider ollama        # → http://127.0.0.1:8000
```

Three views: **Schema** browses the map (tables → columns with inferred meanings, types, keys and
relationships, with search), **Logic** lists what each stored procedure/function/package/trigger/
view does (plus the tables it touches), and **Ask** runs the natural-language → SQL loop — you see
the SQL (editable), the assumptions and confidence, then the results. The server binds to **localhost only**
by default and keeps every boundary the CLI does: the model sees only the map, queries are validated
read-only before running, and results stay in your browser (never sent to the model).

## See an example

A full generated database map (from the bundled demo schema) is committed so you can see the output
before running anything: **[examples/database_map.demo.md](examples/database_map.demo.md)** (and the
machine-readable [JSON](examples/database_map.demo.json)).

## Measure it on a real schema

How good is the reconstruction, really? Blossa can be scored against a *documented* schema used as
ground truth: capture the real comments + FKs, strip them to simulate a legacy estate, scan, and
measure how much Blossa recovers (FK rediscovery rate + documentation coverage). See
**[samples/README.md](samples/README.md)** for the full workflow against the official Oracle sample
schemas (HR / OE).

## Scope (MVP)

In: read-only Oracle introspection (single, multi-, or all-schema), deterministic schema analysis
incl. self / composite / cross-schema FK inference, PII-safe summaries, a local-LLM semantic pass,
Markdown + JSON output, a single-shot natural-language → read-only SQL command (`ask`), and an
optional local web UI (`serve`) to browse the map and ask questions.

Out (for now): multi-turn conversational chat, any write access, non-Oracle engines,
query-log/lineage ingestion, managed cloud, model fine-tuning.

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
