#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
FIXTURE_REPO="${ROOT}/workspaces/.fixtures/transaction-fixture"

[[ -x "${REPO}/scripts/orchestra-integrator.py" ]]
[[ -x "${REPO}/scripts/orchestra-transaction.py" ]]
[[ -f "${REPO}/migrations/006_reviewed_integration.sql" ]]
[[ -f "${REPO}/docs/INTEGRATION.md" ]]

python3 -m py_compile \
    "${REPO}/scripts/orchestra-integrator.py" \
    "${REPO}/scripts/orchestra-transaction.py"

"${REPO}/scripts/orchestra-integrator.py" self-test

grep -Fq 'def validate_review' \
    "${REPO}/scripts/orchestra-integrator.py"
grep -Fq 'Review is stale' \
    "${REPO}/scripts/orchestra-integrator.py"
grep -Fq 'merge",' \
    "${REPO}/scripts/orchestra-integrator.py"
grep -Fq '"--ff-only"' \
    "${REPO}/scripts/orchestra-integrator.py"
grep -Fq 'snapshot_verified=True' \
    "${REPO}/scripts/orchestra-integrator.py"
grep -Fq 'WAITING_HUMAN' \
    "${REPO}/scripts/orchestra-transaction.py"
grep -Fq "UPDATE approvals" \
    "${REPO}/scripts/orchestra-transaction.py"

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
[[ "$(sqlite3 "$DB" 'PRAGMA user_version;')" == "$LATEST_MIGRATION_NUMBER" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM sqlite_master
         WHERE type='table'
           AND name='integration_executions';"
)" == "1" ]]

sqlite3 "$DB" 'PRAGMA foreign_key_check;' |
grep -q . && {
    echo "SQLite foreign_key_check failed." >&2
    exit 1
}

[[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" == "ok" ]]

BAD_COMPLETED="$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM integration_executions AS ie
         JOIN runs AS r
           ON r.run_id = ie.run_id
         WHERE ie.status = 'COMPLETED'
           AND (
               ie.decision <> 'APPROVE'
               OR ie.verdict NOT IN ('PASS', 'PASS_WITH_DEBT')
               OR ie.snapshot_verified <> 1
               OR ie.review_current <> 1
               OR ie.main_after <> ie.reviewed_commit
               OR r.status <> 'COMPLETED'
           );"
)"
[[ "$BAD_COMPLETED" == "0" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM integration_executions
         WHERE status='COMPLETED';"
)" -ge 1 ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM integration_executions
         WHERE status='REJECTED'
           AND decision='REJECT';"
)" -ge 1 ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM integration_executions
         WHERE status='BLOCKED'
           AND decision='BLOCK_HUMAN';"
)" -ge 1 ]]

LATEST_COMPLETED_BASE="$(
    sqlite3 "$DB" \
        "SELECT base_commit
         FROM integration_executions
         WHERE status='COMPLETED'
         ORDER BY created_at DESC
         LIMIT 1;"
)"
[[ -n "$LATEST_COMPLETED_BASE" ]]
[[ "$(git -C "$FIXTURE_REPO" rev-parse HEAD)" == "$LATEST_COMPLETED_BASE" ]]
[[ -z "$(
    git -C "$FIXTURE_REPO" \
        status --porcelain=v1 --untracked-files=all
)" ]]
[[ ! -e "${FIXTURE_REPO}/worker-result.txt" ]]

[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]
[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM projects WHERE enabled=1;')" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM approvals
         WHERE status='PENDING';"
)" == "0" ]]

echo "Orchestra reviewed integration foundation: PASS"
