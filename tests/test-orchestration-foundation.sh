#!/usr/bin/env bash
set -Eeuo pipefail

orchestration_foundation_error() {
    local rc=$?
    echo "ORCHESTRATION FOUNDATION FAILURE" >&2
    echo "Line    : ${BASH_LINENO[0]:-${LINENO}}" >&2
    echo "Command : ${BASH_COMMAND}" >&2
    echo "Exit    : ${rc}" >&2
    exit "$rc"
}
trap orchestration_foundation_error ERR

orchestration_stage() {
    echo "Orchestration foundation stage: $1"
}

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
FIXTURE_REPO="${ROOT}/workspaces/.fixtures/transaction-fixture"

orchestration_stage "required files"
for file in \
    "${REPO}/scripts/orchestra-orchestrator.py" \
    "${REPO}/scripts/orchestra-planner.py" \
    "${REPO}/scripts/orchestra-planner-entry.py" \
    "${REPO}/migrations/009_orchestration_dag.sql" \
    "${REPO}/config/orchestrator.toml" \
    "${REPO}/compose/agent.yaml" \
    "${REPO}/docs/ORCHESTRATION.md" \
    "${REPO}/tests/test-orchestration-foundation.sh"
do
    [[ -f "$file" ]]
done

orchestration_stage "python compilation"
python3 -m py_compile \
    "${REPO}/scripts/orchestra-recovery.py" \
    "${REPO}/scripts/orchestra-orchestrator.py" \
    "${REPO}/scripts/orchestra-planner.py" \
    "${REPO}/scripts/orchestra-planner-entry.py"

orchestration_stage "component self-tests"
"${REPO}/scripts/orchestra-recovery.py" self-test
"${REPO}/scripts/orchestra-orchestrator.py" self-test
"${REPO}/scripts/orchestra-planner.py" self-test

orchestration_stage "source contracts"
grep -Fq 'ORCHESTRA_ACTIVE_TASK_SANDBOX_PROTECTION_V1' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'active_task_ids = {' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'orchestra-task-id' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'if container_task_id in active_task_ids:' \
    "${REPO}/scripts/orchestra-recovery.py"

grep -Fq 'def validate_plan' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'def topological_order' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'Same-project PIPELINE tasks must be dependency-ordered' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'ThreadPoolExecutor' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'orchestra-worker.py' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'orchestra-reviewer.py' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'orchestra-integrator.py' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'def launch_reviewer_with_transport_retry' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'def is_transient_review_transport_failure' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'ORCHESTRATION_REVIEW_TRANSPORT_RETRY' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'review_transport_attempts' \
    "${REPO}/config/orchestrator.toml"
grep -Fq 'review_retry_backoff_seconds' \
    "${REPO}/config/orchestrator.toml"
grep -Fq 'orchestrator process restarted' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq '  orchestrator:' "${REPO}/compose/agent.yaml"
grep -Fq 'container_name: orchestra-orchestrator' "${REPO}/compose/agent.yaml"
grep -Fq 'condition: service_healthy' "${REPO}/compose/agent.yaml"
grep -Fq 'no-new-privileges:true' "${REPO}/compose/agent.yaml"
grep -Fq '/run/orchestra-docker' "${REPO}/compose/agent.yaml"

orchestration_stage "database schema"
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

for table in \
    orchestrator_instances \
    orchestration_plans \
    orchestrator_executions \
    orchestration_tasks \
    orchestration_dependencies \
    orchestration_attempts
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

orchestration_stage "orchestrator service"
"${REPO}/scripts/orchestra-compose.sh" ps --status running --services |
    grep -Fxq orchestrator

orchestration_stage "daemon status"
DAEMON_STATUS="$(
    "${REPO}/scripts/orchestra-orchestrator.py" daemon-status
)"
python3 - "$DAEMON_STATUS" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["version"] == "orchestrator-v2"
assert payload["lock_held"] is True
assert payload["supervisor_healthy"] is True
assert payload["instance"]["status"] == "RUNNING"
PY

orchestration_stage "historical orchestration proofs"
# ORCHESTRA_4A_AUDIT_PLAN_SELECTOR_V1
#
# 4A created one controlled AI plan as a cancelled audit artifact. Future
# milestones legitimately create newer AI plans with other terminal states,
# so the foundation test must select the 4A artifact by its own immutable
# objective contract rather than by global recency.
AI_PLAN="$(
    sqlite3 "$DB" \
        "SELECT plan.plan_id
         FROM orchestration_plans AS plan
         WHERE plan.source='AI'
           AND plan.status='CANCELLED'
           AND plan.objective LIKE
               'Prepare a minimal reviewed implementation for the transaction fixture%'
           AND EXISTS (
               SELECT 1
               FROM orchestrator_executions AS execution
               WHERE execution.plan_id=plan.plan_id
                 AND execution.role_id='orchestrator'
                 AND execution.source_profile='ops-orchestrator'
                 AND execution.exit_code=0
           )
         ORDER BY plan.created_at DESC
         LIMIT 1;"
)"
[[ -n "$AI_PLAN" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT source || '|' || status
         FROM orchestration_plans
         WHERE plan_id='${AI_PLAN}';"
)" == "AI|CANCELLED" ]]

SUCCESS_PLAN="$(
    sqlite3 "$DB" \
        "SELECT plan_id
         FROM orchestration_plans
         WHERE objective='Prove bounded parallel scheduling followed by a reviewed transactional integration.'
           AND status='COMPLETED'
         ORDER BY created_at DESC
         LIMIT 1;"
)"
[[ -n "$SUCCESS_PLAN" ]]

python3 - "$DB" "$SUCCESS_PLAN" <<'PY'
import sqlite3
import sys
from datetime import datetime

connection = sqlite3.connect(sys.argv[1])
connection.row_factory = sqlite3.Row
rows = connection.execute(
    """
    SELECT task_key, started_at, finished_at
    FROM orchestration_tasks
    WHERE plan_id = ?
      AND task_key IN ('parallel_a', 'parallel_b')
    ORDER BY task_key
    """,
    (sys.argv[2],),
).fetchall()
assert len(rows) == 2
for row in rows:
    assert row["started_at"]
    assert row["finished_at"]

def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

starts = [parse(row["started_at"]) for row in rows]
finishes = [parse(row["finished_at"]) for row in rows]
assert max(starts) < min(finishes), (starts, finishes)
PY

PIPELINE_RESULT="$(
    sqlite3 "$DB" \
        "SELECT result_json
         FROM orchestration_tasks
         WHERE plan_id='${SUCCESS_PLAN}'
           AND task_key='reviewed_integration'
           AND status='COMPLETED';"
)"
python3 - "$PIPELINE_RESULT" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["kind"] == "PIPELINE"
assert payload["integration"]["status"] == "COMPLETED"
assert payload["integration"]["integrated"] is True
assert payload["reviewer"]["decision"] == "APPROVE"
assert payload["reviewer"]["verdict"] in {"PASS", "PASS_WITH_DEBT"}
PY

FAILURE_PLAN="$(
    sqlite3 "$DB" \
        "SELECT plan_id
         FROM orchestration_plans
         WHERE objective='Prove dependency blocking after a terminal task failure.'
           AND status='FAILED'
         ORDER BY created_at DESC
         LIMIT 1;"
)"
[[ -n "$FAILURE_PLAN" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT status
         FROM orchestration_tasks
         WHERE plan_id='${FAILURE_PLAN}'
           AND task_key='intentional_failure';"
)" == "FAILED" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT status
         FROM orchestration_tasks
         WHERE plan_id='${FAILURE_PLAN}'
           AND task_key='must_be_blocked';"
)" == "BLOCKED" ]]

RESUME_PLAN="$(
    sqlite3 "$DB" \
        "SELECT plan_id
         FROM orchestration_plans
         WHERE objective='Prove durable retry after the orchestrator process is killed.'
           AND status='COMPLETED'
         ORDER BY created_at DESC
         LIMIT 1;"
)"
[[ -n "$RESUME_PLAN" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM orchestration_attempts AS attempt
         JOIN orchestration_tasks AS task
           ON task.orchestration_task_id=attempt.orchestration_task_id
         WHERE task.plan_id='${RESUME_PLAN}'
           AND attempt.status='ABANDONED';"
)" -ge 1 ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM orchestration_attempts AS attempt
         JOIN orchestration_tasks AS task
           ON task.orchestration_task_id=attempt.orchestration_task_id
         WHERE task.plan_id='${RESUME_PLAN}'
           AND attempt.status='COMPLETED';"
)" -ge 1 ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM orchestration_plans
         WHERE status IN ('READY', 'RUNNING', 'BLOCKED');"
)" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM orchestration_tasks
         WHERE status IN ('PENDING', 'READY', 'RUNNING');"
)" == "0" ]]
orchestration_stage "production registry invariants"
[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]

# ORCHESTRA_ORCHESTRATION_REAL_PROJECT_REGISTRY_COMPATIBILITY_V1
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM projects
         WHERE project_id LIKE 'transaction-fixture%'
           AND enabled != 0;"
)" == "0" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM projects
         WHERE enabled = 1
           AND (
               TRIM(repo_path) = ''
               OR TRIM(data_path) = ''
               OR TRIM(policy_id) = ''
               OR TRIM(config_source) = ''
               OR TRIM(config_hash) = ''
           );"
)" == "0" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM projects
         WHERE enabled = 1
           AND project_id NOT LIKE 'transaction-fixture%';"
)" -ge 1 ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*) FROM approvals WHERE status='PENDING';"
)" == "0" ]]

orchestration_stage "fixture integrity"
[[ "$(git -C "$FIXTURE_REPO" branch --show-current)" == "main" ]]
[[ -z "$(
    git -C "$FIXTURE_REPO" \
        status --porcelain=v1 --untracked-files=all
)" ]]
[[ ! -e "${FIXTURE_REPO}/orchestration-result.txt" ]]

orchestration_stage "runtime cleanup"
if find "${ROOT}/workspaces/.orchestra-worker-clones" \
    -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    echo "Clone worker résiduel après orchestration." >&2
    exit 1
fi

if find "${ROOT}/workspaces/.orchestra-reviewer-clones" \
    -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    echo "Clone reviewer résiduel après orchestration." >&2
    exit 1
fi

echo "Orchestra persistent multi-task orchestration foundation: PASS"
