#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
[[ -x "${REPO}/scripts/orchestra-supervisor.py" ]]
[[ -x "${REPO}/scripts/orchestra-recovery.py" ]]
[[ -f "${REPO}/migrations/008_supervisor_watchdog.sql" ]]
[[ -f "${REPO}/config/supervisor.toml" ]]
[[ -f "${REPO}/compose/agent.yaml" ]]
[[ -f "${REPO}/docs/SUPERVISOR.md" ]]

python3 -m py_compile \
    "${REPO}/scripts/orchestra-supervisor.py"

"${REPO}/scripts/orchestra-supervisor.py" self-test

grep -Fq 'def acquire_lock' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq 'fcntl.LOCK_EX | fcntl.LOCK_NB' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq 'def wait_for_core_health' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq 'orchestra-sandbox-engine' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq 'orchestra-hermes-agent' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq '"startup"' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq '"periodic"' \
    "${REPO}/scripts/orchestra-supervisor.py"
grep -Fq '  supervisor:' "${REPO}/compose/agent.yaml"
grep -Fq 'container_name: orchestra-supervisor' "${REPO}/compose/agent.yaml"
grep -Fq 'no-new-privileges:true' "${REPO}/compose/agent.yaml"
grep -Fq '/run/orchestra-docker' "${REPO}/compose/agent.yaml"

LATEST_MIGRATION_FILE="$(
    find "${REPO}/migrations" \
        -maxdepth 1 \
        -type f \
        -name '[0-9][0-9][0-9]_*.sql' \
        -printf '%f\n' |
    sort |
    tail -n 1
)"
[[ -n "$LATEST_MIGRATION_FILE" ]]
LATEST_MIGRATION_NUMBER="$((10#${LATEST_MIGRATION_FILE%%_*}))"
[[ "$(sqlite3 "$DB" 'PRAGMA user_version;')" == \
   "$LATEST_MIGRATION_NUMBER" ]]

for table in supervisor_instances supervisor_sweeps
do
    [[ "$(
        sqlite3 "$DB" \
            "SELECT COUNT(*)
             FROM sqlite_master
             WHERE type='table'
               AND name='${table}';"
    )" == "1" ]]
done

sqlite3 "$DB" 'PRAGMA foreign_key_check;' |
grep -q . && {
    echo "SQLite foreign_key_check failed." >&2
    exit 1
}
[[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" == "ok" ]]

"${REPO}/scripts/orchestra-compose.sh" ps --status running --services |
    grep -Fxq supervisor

supervisor_status_ready() {
    local payload
    payload="$(
        "${REPO}/scripts/orchestra-supervisor.py" status 2>/dev/null
    )" || return 1

    python3 - "$payload" >/dev/null 2>&1 <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
    last_sweep = payload.get("last_sweep") or {}
    ready = (
        payload.get("version") == "supervisor-v1"
        and payload.get("lock_held") is True
        and (payload.get("health") or {}).get("healthy") is True
        and (payload.get("instance") or {}).get("status") == "RUNNING"
        and last_sweep.get("status") in {"COMPLETED", "SKIPPED"}
    )
except Exception:
    ready = False

raise SystemExit(0 if ready else 1)
PY
}

SUPERVISOR_READY=0
for _ in $(seq 1 60)
do
    if supervisor_status_ready; then
        SUPERVISOR_READY=1
        break
    fi
    sleep 1
done

if [[ "$SUPERVISOR_READY" != "1" ]]; then
    "${REPO}/scripts/orchestra-supervisor.py" status >&2 || true
    echo "Supervisor non stable après attente du sweep terminal." >&2
    exit 1
fi

STATUS_JSON="$(
    "${REPO}/scripts/orchestra-supervisor.py" status
)"
python3 - "$STATUS_JSON" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["version"] == "supervisor-v1"
assert payload["lock_held"] is True
assert payload["health"]["healthy"] is True
assert payload["instance"]["status"] == "RUNNING"
assert payload["last_sweep"]["status"] in {
    "COMPLETED",
    "SKIPPED",
}
PY

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM supervisor_sweeps
         WHERE status='COMPLETED';"
)" -ge 1 ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM supervisor_instances
         WHERE status='ABANDONED';"
)" -ge 1 ]]

[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]
[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM projects WHERE enabled=1;')" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM approvals
         WHERE status='PENDING';"
)" == "0" ]]

echo "Orchestra automatic supervisor foundation: PASS"
