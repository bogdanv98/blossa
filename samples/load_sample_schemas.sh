#!/usr/bin/env bash
# Download the official Oracle sample schemas and stage them inside the running
# blossa-oracle container. The final install step uses Oracle's own installer
# (which prompts for a few values), so it stays reliable across image versions.
#
# Prereqs:
#   - The demo container is up:  (cd docker && docker compose up -d)  -- wait until healthy.
#   - git available locally.
#
# Usage:  bash samples/load_sample_schemas.sh
set -euo pipefail

CONTAINER="blossa-oracle"
REPO="https://github.com/oracle-samples/db-sample-schemas.git"
DEST="samples/db-sample-schemas"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "ERROR: container '${CONTAINER}' is not running."
  echo "       Start it first:  cd docker && docker compose up -d   (wait until healthy)"
  exit 1
fi

if [ ! -d "${DEST}" ]; then
  echo ">> Cloning official Oracle sample schemas into ${DEST} ..."
  git clone --depth 1 "${REPO}" "${DEST}"
else
  echo ">> ${DEST} already present, skipping clone."
fi

echo ">> Copying sample schemas into the container at /tmp/db-sample-schemas ..."
docker cp "${DEST}" "${CONTAINER}:/tmp/db-sample-schemas"
docker cp "samples/legacy_ify.sql" "${CONTAINER}:/tmp/legacy_ify.sql"

cat <<'EOF'

>> Staged. Now install a schema with Oracle's installer (HR is the simplest, fully
   self-contained option; OE is larger). Connect to the container as SYSTEM and run it:

   docker exec -it blossa-oracle bash
   cd /tmp/db-sample-schemas/human_resources   # path may differ per repo version
   sqlplus system/oracle@//localhost:1521/XEPDB1

   Then inside SQL*Plus run the install script for HR and answer the prompts
   (set the HR password to "oracle" so samples/hr.yml works out of the box).

>> After HR is installed, run the evaluation workflow:

   blossa ground-truth -c samples/hr.yml -o samples/hr_truth.json     # capture the truth FIRST
   docker exec -i blossa-oracle sqlplus HR/oracle@//localhost:1521/XEPDB1 @/tmp/legacy_ify.sql
   blossa scan -c samples/hr.yml --llm-provider ollama                # or heuristic
   blossa eval -t samples/hr_truth.json -s samples/out/hr_map.json

EOF
