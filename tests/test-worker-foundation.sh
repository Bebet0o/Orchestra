#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
PRIVATE_DOCKER_HOST="unix://${ROOT}/runtime/sandbox-engine-socket/docker.sock"

[[ -x "${REPO}/scripts/orchestra-worker.py" ]]
[[ -x "${REPO}/scripts/orchestra-worker-entry.py" ]]

grep -Fq 'ORCHESTRA_SANDBOX_AUTOMOUNTS_DISABLED' \
    "${REPO}/scripts/orchestra-worker-entry.py"

grep -Fq 'ORCHESTRA_PRECREATED_SANDBOX_REUSE' \
    "${REPO}/scripts/orchestra-worker-entry.py"

grep -Fq 'def precreate_worker_sandbox' \
    "${REPO}/scripts/orchestra-worker.py"

[[ -f "${REPO}/migrations/004_worker_executions.sql" ]]
[[ -f "${REPO}/config/environments/default-worker.toml" ]]
[[ -f "${REPO}/images/orchestra-worker.Dockerfile" ]]

LATEST_MIGRATION_FILE="$(
    find "${REPO}/migrations"         -maxdepth 1         -type f         -name '[0-9][0-9][0-9]_*.sql'         -printf '%f\n' |
    sort |
    tail -n 1
)"

[[ -n "$LATEST_MIGRATION_FILE" ]]

LATEST_MIGRATION_NUMBER="$((
    10#${LATEST_MIGRATION_FILE%%_*}
))"

[[ "$(
    sqlite3 "$DB" 'PRAGMA user_version;'
)" == "$LATEST_MIGRATION_NUMBER" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM sqlite_master
         WHERE type='table'
           AND name='worker_executions';"
)" == "1" ]]

EXECUTION_ID="$(
    sqlite3 "$DB" \
        "SELECT execution_id
         FROM worker_executions
         WHERE role_id='worker_code'
           AND mount_verified=1
           AND isolation_verified=1
           AND exit_code=0
         ORDER BY created_at DESC
         LIMIT 1;"
)"

[[ -n "$EXECUTION_ID" ]]

TASK_STATUS="$(
    sqlite3 "$DB" \
        "SELECT t.status
         FROM worker_executions AS w
         JOIN tasks AS t
           ON t.task_id=w.task_id
         WHERE w.execution_id='${EXECUTION_ID}';"
)"

[[ "$TASK_STATUS" == "COMPLETED" ]]

RUNTIME_PROFILE="$(
    sqlite3 "$DB" \
        "SELECT runtime_profile
         FROM worker_executions
         WHERE execution_id='${EXECUTION_ID}';"
)"

[[ ! -e "${ROOT}/state/hermes-home/profiles/${RUNTIME_PROFILE}" ]]

OUTER_CONTAINER="$(
    sqlite3 "$DB" \
        "SELECT outer_container_name
         FROM worker_executions
         WHERE execution_id='${EXECUTION_ID}';"
)"

if docker --host "$PRIVATE_DOCKER_HOST" \
    container inspect "$OUTER_CONTAINER" >/dev/null 2>&1; then
    echo "Conteneur worker externe résiduel." >&2
    exit 1
fi

SANDBOX_ID="$(
    sqlite3 "$DB" \
        "SELECT sandbox_container_id
         FROM worker_executions
         WHERE execution_id='${EXECUTION_ID}';"
)"

if docker --host "$PRIVATE_DOCKER_HOST" \
    container inspect "$SANDBOX_ID" >/dev/null 2>&1; then
    echo "Sandbox worker résiduelle." >&2
    exit 1
fi

OUTPUT_PATH="$(
    sqlite3 "$DB" \
        "SELECT output_path
         FROM worker_executions
         WHERE execution_id='${EXECUTION_ID}';"
)"

[[ -s "$OUTPUT_PATH" ]]

RESULT_JSON="$(
    sqlite3 "$DB" \
        "SELECT result_json
         FROM worker_executions
         WHERE execution_id='${EXECUTION_ID}';"
)"

python3 - "$RESULT_JSON" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["exit_code"] == 0
assert payload["marker_found"] is True
assert payload["standalone_clone"] is True
assert payload["protected_refs_verified"] is True
assert payload["sandbox_preflight"]["clean"] is True
assert payload["sandbox_audit"]["network_mode"] == "none"
assert payload["sandbox_audit"]["workspace_rw"] is True
assert payload["sandbox_audit"]["sensitive_env_count"] == 0
PY

IMAGE_REFERENCE="$(
    python3 - <<'PY'
import tomllib
from pathlib import Path

path = Path(
    "/opt/orchestra/repo/config/environments/default-worker.toml"
)

with path.open("rb") as stream:
    print(tomllib.load(stream)["image_reference"])
PY
)"

docker --host "$PRIVATE_DOCKER_HOST" image inspect "$IMAGE_REFERENCE" >/dev/null

[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT enabled
         FROM projects
         WHERE project_id='transaction-fixture';"
)" == "0" ]]

if find "${ROOT}/workspaces/.orchestra-worker-clones" \
    -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    echo "Clone worker résiduel." >&2
    exit 1
fi

echo "Orchestra controlled worker foundation: PASS"
