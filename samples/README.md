# Testing Blossa against real Oracle sample schemas

Blossa is built to reverse-engineer **undocumented** schemas — but to *measure* how well it does
that, we need a schema where the truth is known. Oracle's official sample schemas (HR, OE, SH, …)
are perfect: they are realistic, they have real relationships, and they are **documented**. So we:

1. **Capture the truth** from the documented schema (`blossa ground-truth`).
2. **"Legacy-ify"** it — drop the foreign keys and strip the comments (`legacy_ify.sql`).
3. **Scan** the now-undocumented schema (`blossa scan`).
4. **Evaluate** what Blossa recovered vs. the truth (`blossa eval`).

The headline metric is **FK rediscovery** (objective): of the relationships we hid, how many did
Blossa re-infer? Plus **documentation coverage**: of the columns/tables that were documented, for
how many did Blossa produce a confident meaning.

## Which schemas

- **HR** — small, fully self-contained, includes a self-referencing FK (`EMPLOYEES.MANAGER_ID`).
  The most reliable starting point; loads anywhere.
- **OE** (Order Entry) — larger, richer relationships. Recommended second. Note: some columns use
  Oracle Locator/spatial types that may be skipped on slim XE images — that's fine for our purposes.
- **SH** (Sales History) — advanced/optional; it loads data via external files and is fiddly in a
  container. Skip unless you specifically want a large star schema for scale testing.

## Quick start (one command)

If you just want the whole thing to run end-to-end, use the orchestrator. It brings up the
container, stages + installs HR non-interactively, captures truth, legacy-ifies, scans, and evals:

```bash
bash samples/run_eval.sh            # heuristic baseline (offline)
bash samples/run_eval.sh ollama     # semantic pass (needs Ollama running + the model pulled)
```

Prereq: Docker Desktop running, and `blossa` installed (`pip install -e .`). The steps below are
the same flow done manually, for when you want to run a single stage at a time.

## Steps

### 0. Bring up the demo Oracle container

```bash
cd docker && docker compose up -d     # wait until healthy
cd ..
```

### 1. Stage the official sample schemas

```bash
bash samples/load_sample_schemas.sh
```

This clones [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas)
and copies it (plus `legacy_ify.sql`) into the container. It then prints the exact command to run
Oracle's installer for HR. Install HR with the password `oracle` so [hr.yml](hr.yml) works as-is.

### 2. Capture ground truth — BEFORE breaking anything

```bash
blossa ground-truth -c samples/hr.yml -o samples/hr_truth.json
```

### 3. Legacy-ify the schema (drop FKs + comments)

```bash
docker exec -i blossa-oracle sqlplus HR/oracle@//localhost:1521/XEPDB1 @/tmp/legacy_ify.sql
```

### 4. Scan the now-undocumented schema

```bash
# Heuristic = fast offline baseline; Ollama = the real semantic pass.
blossa scan -c samples/hr.yml --llm-provider ollama
```

### 5. Evaluate

```bash
blossa eval -t samples/hr_truth.json -s samples/out/hr_map.json
```

You'll get a scorecard: FK recall/precision and documentation coverage, plus a list of any real
foreign keys Blossa failed to rediscover — exactly the cases worth improving next.

> Tip: run step 5 once with `--llm-provider heuristic` and once with `ollama` to see how much the
> language model adds over the deterministic baseline.

## Note

These are throwaway sample databases meant for testing. `legacy_ify.sql` is **destructive** — only
ever run it against sample/throwaway schemas, never real data.
