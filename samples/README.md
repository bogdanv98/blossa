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

## Composite-FK test schema (synthetic)

None of Oracle's sample schemas declare a **composite** (multi-column) foreign key, so there is
nothing to measure composite-FK rediscovery against. [composite_demo.sql](composite_demo.sql)
creates a tiny synthetic schema (`CFKDEMO`) with two real composite FKs into `ORDER_ITEMS`:
`ITEM_RETURNS(order_id, item_no)` (rediscovered by **exact column name**) and
`RETURN_LINES(source_order_id, return_item_no)` (role-named columns, rediscovered by **name
suffix + type + tuple overlap**). Run it through the same flow:

```bash
docker cp samples/composite_demo.sql blossa-oracle:/tmp/composite_demo.sql
docker exec -i blossa-oracle sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/composite_demo.sql
blossa ground-truth -c samples/cfk.yml -o samples/cfk_truth.json
docker exec -i blossa-oracle sqlplus -s CFKDEMO/oracle@//localhost:1521/XEPDB1 @/tmp/legacy_ify.sql
blossa scan -c samples/cfk.yml --llm-provider heuristic
blossa eval -t samples/cfk_truth.json -s samples/out/cfk_map.json
```

(On Windows/Git Bash, prefix the `docker exec ... @/tmp/...` lines with `MSYS_NO_PATHCONV=1`.)

## Multi-schema scanning + cross-schema FKs

A scan can cover more than one schema at once. In a config's `oracle.schema`, pass a single name
(as before), a **list** (`["HR", "OE"]`), or `"*"` for **every non-system schema**. With several
schemas in scope, foreign keys that point *across* schemas become rediscoverable — a single-schema
scan can't find them because the parent key lives in a schema it never looked at.

[cross_schema_demo.sql](cross_schema_demo.sql) builds a synthetic `EXTSALES` schema with two
cross-schema FKs into HR (`SALES_CONTACTS.location_id -> HR.LOCATIONS`, exact name;
`SALES_CONTACTS.rep_id -> HR.EMPLOYEES`, by `_ID` suffix + data). Capture truth on EXTSALES alone,
strip it, then scan **both** schemas with [crossscan.yml](crossscan.yml) (`schema: ["HR","EXTSALES"]`):

```bash
docker cp samples/cross_schema_demo.sql blossa-oracle:/tmp/cross_schema_demo.sql
docker exec -i blossa-oracle sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/cross_schema_demo.sql
blossa ground-truth -c samples/extsales.yml -o samples/extsales_truth.json
docker exec -i blossa-oracle sqlplus -s EXTSALES/oracle@//localhost:1521/XEPDB1 @/tmp/legacy_ify.sql
blossa scan -c samples/crossscan.yml --llm-provider heuristic
blossa eval -t samples/extsales_truth.json -s samples/out/cross_map.json   # FK recall = 100% (2/2)
```

Cross-schema relationships are flagged `(cross-schema)` in the map and carry `from_owner`/`to_owner`
in the JSON. (On Windows/Git Bash, prefix the `docker exec ... @/tmp/...` lines with `MSYS_NO_PATHCONV=1`.)

## Note

These are throwaway sample databases meant for testing. `legacy_ify.sql` is **destructive** — only
ever run it against sample/throwaway schemas, never real data.
