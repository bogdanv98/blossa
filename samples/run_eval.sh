#!/usr/bin/env bash
# One-shot evaluation run for Blossa against the official Oracle HR sample schema.
#
# Does EVERYTHING end-to-end so you don't have to step through it manually:
#   1. Bring up the demo Oracle container and wait until it is healthy.
#   2. Clone + stage the official Oracle sample schemas into the container.
#   3. Install HR *non-interactively* (the installer's prompts are fed automatically).
#   4. Capture ground truth (BEFORE breaking anything).
#   5. "Legacy-ify" HR: drop foreign keys + strip comments.
#   6. Scan the now-undocumented schema.
#   7. Evaluate the scan vs. the captured truth.
#
# Prereqs:
#   - Docker Desktop is RUNNING.
#   - `blossa` is installed/importable from this directory (pip install -e .).
#   - git available locally.
#
# Usage:
#   bash samples/run_eval.sh                 # heuristic provider (fast, offline)
#   bash samples/run_eval.sh ollama          # real semantic pass (needs Ollama up)
set -euo pipefail

PROVIDER="${1:-heuristic}"
CONTAINER="blossa-oracle"
REPO="https://github.com/oracle-samples/db-sample-schemas.git"
DEST="samples/db-sample-schemas"
HR_PASS="oracle"                 # keep in sync with samples/hr.yml
SYS_CONN="system/oracle@//localhost:1521/XEPDB1"
HR_CONN="HR/${HR_PASS}@//localhost:1521/XEPDB1"

say() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }

# --- Make the `blossa` CLI reachable (use the local venv if present) ----------
if ! command -v blossa >/dev/null 2>&1; then
  if [ -x ".venv/Scripts/blossa.exe" ]; then       # Windows venv
    export PATH="${PWD}/.venv/Scripts:${PATH}"
  elif [ -x ".venv/bin/blossa" ]; then              # POSIX venv
    export PATH="${PWD}/.venv/bin:${PATH}"
  else
    echo "ERROR: 'blossa' not found. Install it first:  pip install -e ."
    exit 1
  fi
fi

# --- 0. Docker daemon up? -----------------------------------------------------
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not reachable. Start Docker Desktop and retry."
  exit 1
fi

# --- 1. Bring up the container and wait for healthy ---------------------------
say "Bringing up the demo Oracle container ..."
( cd docker && docker compose up -d )

say "Waiting for ${CONTAINER} to become healthy (first boot can take a few minutes) ..."
for i in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "${CONTAINER}" 2>/dev/null || echo missing)"
  printf '   [%02d/60] health=%s\n' "$i" "$status"
  [ "$status" = "healthy" ] && break
  if [ "$i" -eq 60 ]; then
    echo "ERROR: container did not become healthy in time. Check: docker logs ${CONTAINER}"
    exit 1
  fi
  sleep 10
done

# --- 2. Clone + stage the official sample schemas -----------------------------
if [ ! -d "${DEST}" ]; then
  say "Cloning official Oracle sample schemas into ${DEST} ..."
  git clone --depth 1 "${REPO}" "${DEST}"
else
  say "${DEST} already present, skipping clone."
fi

say "Staging sample schemas + helper SQL into the container ..."
# MSYS_NO_PATHCONV stops Git Bash on Windows from mangling the /tmp/... paths.
MSYS_NO_PATHCONV=1 docker cp "${DEST}" "${CONTAINER}:/tmp/db-sample-schemas"
MSYS_NO_PATHCONV=1 docker cp "samples/legacy_ify.sql" "${CONTAINER}:/tmp/legacy_ify.sql"
MSYS_NO_PATHCONV=1 docker cp "samples/hr_install_auto.sql" "${CONTAINER}:/tmp/hr_install_auto.sql"

# --- 3. Install HR non-interactively ------------------------------------------
# Oracle's hr_install.sql uses ACCEPT/HIDE prompts that are unreliable over a
# pipe, so we use a deterministic wrapper (hr_install_auto.sql) that creates the
# HR user and calls the prompt-free sub-scripts directly.
say "Installing HR (deterministic, non-interactive) ..."
docker exec -i "${CONTAINER}" bash -lc "
  cd /tmp/db-sample-schemas/human_resources &&
  sqlplus -s ${SYS_CONN} @/tmp/hr_install_auto.sql
"

# --- 4. Capture ground truth FIRST --------------------------------------------
say "Capturing ground truth (real FKs + comments) BEFORE breaking anything ..."
blossa ground-truth -c samples/hr.yml -o samples/hr_truth.json

# --- 5. Legacy-ify: drop FKs + strip comments ---------------------------------
say "Legacy-ifying HR (dropping FKs, clearing comments) ..."
# Wrap in bash -lc + MSYS_NO_PATHCONV so Git Bash doesn't rewrite the @/tmp path.
MSYS_NO_PATHCONV=1 docker exec -i "${CONTAINER}" bash -lc \
  "sqlplus -s ${HR_CONN} @/tmp/legacy_ify.sql"

# --- 6. Scan the now-undocumented schema --------------------------------------
say "Scanning with provider='${PROVIDER}' ..."
blossa scan -c samples/hr.yml --llm-provider "${PROVIDER}"

# --- 7. Evaluate --------------------------------------------------------------
say "Evaluating scan vs. ground truth ..."
blossa eval -t samples/hr_truth.json -s samples/out/hr_map.json

say "Done. Re-run with a different provider to compare, e.g.: bash samples/run_eval.sh ollama"
