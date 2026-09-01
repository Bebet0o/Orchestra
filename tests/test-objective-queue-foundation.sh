#!/usr/bin/env bash
set -Eeuo pipefail

objective_foundation_error() {
    local rc=$?
    echo "OBJECTIVE FOUNDATION FAILURE" >&2
    echo "Line    : ${BASH_LINENO[0]:-${LINENO}}" >&2
    echo "Command : ${BASH_COMMAND}" >&2
    echo "Exit    : ${rc}" >&2
    exit "$rc"
}
trap objective_foundation_error ERR

objective_stage() {
    echo "Objective foundation stage: $1"
}

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"

objective_stage "required files"
for file in \
    "${REPO}/scripts/orchestra-objectives.py" \
    "${REPO}/scripts/orchestra-orchestrator.py" \
    "${REPO}/migrations/010_objective_queue.sql" \
    "${REPO}/config/orchestrator.toml" \
    "${REPO}/docs/OBJECTIVES.md" \
    "${REPO}/tests/test-objective-queue-foundation.sh"
do
    if [[ ! -f "$file" ]]; then
        echo "Required objective foundation file missing: $file" >&2
        exit 1
    fi
done

objective_stage "python compilation"
python3 -m py_compile \
    "${REPO}/scripts/orchestra-objectives.py" \
    "${REPO}/scripts/orchestra-orchestrator.py"

objective_stage "component self-tests"
"${REPO}/scripts/orchestra-objectives.py" self-test
"${REPO}/scripts/orchestra-orchestrator.py" self-test

objective_stage "source contracts"
grep -Fq 'def synchronize_objective_states' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'def reserve_ai_objective' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'def reconcile_interrupted_planner_executions' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'Preserve global priority' \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq "objective.status = 'RUNNING'" \
    "${REPO}/scripts/orchestra-orchestrator.py"
grep -Fq 'global_parallel_objectives = 2' \
    "${REPO}/config/orchestrator.toml"
grep -Fq 'planning_retry_backoff_seconds = 30' \
    "${REPO}/config/orchestrator.toml"

objective_stage "database schema"
# ORCHESTRA_OBJECTIVE_QUEUE_MINIMUM_MIGRATION_V1
OBJECTIVE_QUEUE_SCHEMA_VERSION="$(
    sqlite3 "$DB" 'PRAGMA user_version;'
)"
[[ "$OBJECTIVE_QUEUE_SCHEMA_VERSION" =~ ^[0-9]+$ ]]
[[ "$OBJECTIVE_QUEUE_SCHEMA_VERSION" -ge 10 ]]
[[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" == "ok" ]]
! sqlite3 "$DB" 'PRAGMA foreign_key_check;' | grep -q .

for table in objective_queue objective_attempts objective_events
do
    [[ "$(
        sqlite3 "$DB" \
            "SELECT COUNT(*) FROM sqlite_master
             WHERE type='table' AND name='${table}';"
    )" == "1" ]]
done

objective_stage "orchestrator service"
"${REPO}/scripts/orchestra-compose.sh" ps --status running --services |
    grep -Fxq orchestrator

objective_stage "daemon status"
DAEMON_STATUS="$("${REPO}/scripts/orchestra-orchestrator.py" daemon-status)"
python3 - "$DAEMON_STATUS" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["version"] == "orchestrator-v2"
assert payload["lock_held"] is True
assert payload["supervisor_healthy"] is True
assert payload["instance"]["status"] == "RUNNING"
assert isinstance(payload["objective_counts"], dict)
PY

objective_stage "state invariants"

REAL_ENABLED_PROJECTS="$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM projects
         WHERE enabled = 1
           AND project_id NOT LIKE 'transaction-fixture%';"
)"

if [[ "$REAL_ENABLED_PROJECTS" == "0" ]]; then
    echo "Objective foundation mode: historical fixtures"
python3 - "$DB" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime

connection = sqlite3.connect(sys.argv[1])
connection.row_factory = sqlite3.Row

objectives = {
    row["objective"]: row
    for row in connection.execute("SELECT * FROM objective_queue")
}

high_name = "High-priority project-B objective used to prove global queue ordering."
low_a_name = "Low-priority project-A objective used to prove cross-project concurrency."
low_b_name = "Low-priority project-B objective that must wait behind the high-priority project-B objective."
pause_name = "Prove that a queued objective can be paused and resumed without dispatch."
cancel_name = "Prove cancellation before dispatch."

for name in (high_name, low_a_name, low_b_name, pause_name, cancel_name):
    assert name in objectives, name

for name in (high_name, low_a_name, low_b_name, pause_name):
    assert objectives[name]["status"] == "COMPLETED", (name, objectives[name]["status"])
assert objectives[cancel_name]["status"] == "CANCELLED"

parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))

def task_for(name: str):
    return connection.execute(
        """
        SELECT task.*
        FROM orchestration_tasks AS task
        JOIN objective_queue AS objective
          ON objective.plan_id = task.plan_id
        WHERE objective.objective = ?
        """,
        (name,),
    ).fetchone()

high = task_for(high_name)
low_a = task_for(low_a_name)
low_b = task_for(low_b_name)
assert high and low_a and low_b
assert parse(objectives[high_name]["created_at"]) > parse(objectives[low_b_name]["created_at"])
assert parse(high["started_at"]) < parse(low_b["started_at"])
assert max(parse(high["started_at"]), parse(low_a["started_at"])) < min(
    parse(high["finished_at"]), parse(low_a["finished_at"])
)
assert parse(high["finished_at"]) <= parse(low_b["started_at"])

pause_id = objectives[pause_name]["objective_id"]
pause_events = {
    row[0]
    for row in connection.execute(
        "SELECT event_type FROM objective_events WHERE objective_id = ?",
        (pause_id,),
    )
}
assert "OBJECTIVE_PAUSED" in pause_events
assert "OBJECTIVE_RESUMED" in pause_events

cancel_plan = objectives[cancel_name]["plan_id"]
assert connection.execute(
    """
    SELECT COUNT(*)
    FROM orchestration_attempts AS attempt
    JOIN orchestration_tasks AS task
      ON task.orchestration_task_id = attempt.orchestration_task_id
    WHERE task.plan_id = ?
    """,
    (cancel_plan,),
).fetchone()[0] == 0

ai = connection.execute(
    """
    SELECT *
    FROM objective_queue
    WHERE source = 'AI'
      AND objective LIKE 'Create exactly one independently reviewable PIPELINE task%'
    ORDER BY created_at DESC
    LIMIT 1
    """
).fetchone()
assert ai is not None
assert ai["status"] == "COMPLETED", ai["status"]
attempt_statuses = [
    row[0]
    for row in connection.execute(
        """
        SELECT status
        FROM objective_attempts
        WHERE objective_id = ?
        ORDER BY attempt_number
        """,
        (ai["objective_id"],),
    )
]
assert "ABANDONED" in attempt_statuses, attempt_statuses
assert "COMPLETED" in attempt_statuses, attempt_statuses

pipeline = connection.execute(
    """
    SELECT result_json
    FROM orchestration_tasks
    WHERE plan_id = ? AND kind = 'PIPELINE' AND status = 'COMPLETED'
    ORDER BY created_at
    LIMIT 1
    """,
    (ai["plan_id"],),
).fetchone()
assert pipeline is not None
result = json.loads(pipeline["result_json"])
assert result["worker"]["exit_code"] == 0
assert result["worker"]["marker_found"] is True
assert result["integration"]["integrated"] is True
assert result["reviewer"]["decision"] == "APPROVE"

active = connection.execute(
    """
    SELECT COUNT(*)
    FROM objective_queue
    WHERE status IN (
        'QUEUED', 'PLANNING', 'RUNNING',
        'PAUSE_REQUESTED', 'CANCEL_REQUESTED'
    )
    """
).fetchone()[0]
assert active == 0, active
PY
else
    echo "Objective foundation mode: production registry"
    python3 - "$DB" <<'PY'
from __future__ import annotations

import json
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.row_factory = sqlite3.Row

allowed_objective_statuses = {
    "QUEUED",
    "PLANNING",
    "RUNNING",
    "PAUSE_REQUESTED",
    "PAUSED",
    "CANCEL_REQUESTED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "BLOCKED",
}

statuses = {
    row[0]
    for row in connection.execute(
        "SELECT DISTINCT status FROM objective_queue"
    )
}
unknown = statuses - allowed_objective_statuses
assert not unknown, sorted(unknown)

active = connection.execute(
    """
    SELECT COUNT(*)
    FROM objective_queue
    WHERE status IN (
        'QUEUED',
        'PLANNING',
        'RUNNING',
        'PAUSE_REQUESTED',
        'CANCEL_REQUESTED'
    )
    """
).fetchone()[0]
assert active == 0, active

# ORCHESTRA_OBJECTIVE_PROJECT_SCOPE_SCHEMA_V1
project_ids = {
    row["project_id"]
    for row in connection.execute(
        "SELECT project_id FROM projects"
    )
}

invalid_scopes = []
unknown_scoped_projects = []

for row in connection.execute(
    """
    SELECT objective_id, project_scope_json, plan_id
    FROM objective_queue
    ORDER BY created_at
    """
):
    try:
        scope = json.loads(row["project_scope_json"])
    except json.JSONDecodeError as error:
        invalid_scopes.append(
            {
                "objective_id": row["objective_id"],
                "reason": f"invalid JSON: {error}",
            }
        )
        continue

    if not isinstance(scope, list):
        invalid_scopes.append(
            {
                "objective_id": row["objective_id"],
                "reason": "scope is not a JSON array",
            }
        )
        continue

    if (
        any(
            not isinstance(project_id, str)
            or not project_id.strip()
            for project_id in scope
        )
        or len(scope) != len(set(scope))
    ):
        invalid_scopes.append(
            {
                "objective_id": row["objective_id"],
                "reason": "scope contains an invalid or duplicate project id",
            }
        )
        continue

    missing = sorted(set(scope) - project_ids)
    if missing:
        unknown_scoped_projects.append(
            {
                "objective_id": row["objective_id"],
                "project_ids": missing,
            }
        )

assert not invalid_scopes, invalid_scopes
assert not unknown_scoped_projects, unknown_scoped_projects

missing_task_projects = connection.execute(
    """
    SELECT
        task.orchestration_task_id,
        task.plan_id,
        task.project_id
    FROM orchestration_tasks AS task
    LEFT JOIN projects AS project
      ON project.project_id = task.project_id
    WHERE task.project_id IS NOT NULL
      AND project.project_id IS NULL
    ORDER BY task.created_at
    """
).fetchall()
assert not missing_task_projects, [
    dict(row)
    for row in missing_task_projects
]

invalid_enabled_projects = connection.execute(
    """
    SELECT *
    FROM projects
    WHERE enabled = 1
      AND (
          TRIM(repo_path) = ''
          OR TRIM(data_path) = ''
          OR TRIM(policy_id) = ''
          OR TRIM(config_source) = ''
          OR TRIM(config_hash) = ''
      )
    """
).fetchall()
assert not invalid_enabled_projects, [
    dict(row)
    for row in invalid_enabled_projects
]

enabled_fixtures = connection.execute(
    """
    SELECT project_id
    FROM projects
    WHERE project_id LIKE 'transaction-fixture%'
      AND enabled != 0
    """
).fetchall()
assert not enabled_fixtures, [
    row["project_id"]
    for row in enabled_fixtures
]

ai_rows = connection.execute(
    """
    SELECT objective_id, status, plan_id
    FROM objective_queue
    WHERE source = 'AI'
    ORDER BY created_at
    """
).fetchall()
for row in ai_rows:
    assert row["status"] in allowed_objective_statuses
    if row["status"] == "COMPLETED":
        assert row["plan_id"]

completed_pipeline_rows = connection.execute(
    """
    SELECT result_json
    FROM orchestration_tasks
    WHERE kind = 'PIPELINE'
      AND status = 'COMPLETED'
      AND result_json IS NOT NULL
    """
).fetchall()

for row in completed_pipeline_rows:
    payload = json.loads(row["result_json"])
    if "worker" in payload:
        assert payload["worker"].get("exit_code") == 0
    if "reviewer" in payload:
        assert payload["reviewer"].get("decision") in {
            "APPROVE",
            "PASS",
            "PASS_WITH_DEBT",
        }
    if "integration" in payload:
        assert payload["integration"].get("integrated") is True
PY
fi


[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]

# ORCHESTRA_REAL_PROJECT_REGISTRY_COMPATIBILITY_V1
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
           );"
)" == "0" ]]

# ORCHESTRA_OBJECTIVE_FOUNDATION_PRODUCTION_STATE_V1
objective_stage "repository integrity"
git -C "$REPO" diff --check

if git -C "$REPO" ls-files -u | grep -q .; then
    echo "Unmerged controller files detected." >&2
    exit 1
fi

echo "Orchestra persistent autonomous objective queue foundation: PASS"
