#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
FIXTURE_ID="transaction-fixture"
FIXTURE_REPO="${ROOT}/workspaces/.fixtures/${FIXTURE_ID}"

[[ -x "${REPO}/scripts/orchestra-transaction.py" ]]
[[ -f "${REPO}/migrations/003_git_transactions.sql" ]]

LATEST_MIGRATION_FILE="$(
    find "${REPO}/migrations"         -maxdepth 1         -type f         -name '[0-9][0-9][0-9]_*.sql'         -printf '%f
' |
    sort |
    tail -n 1
)"

[[ -n "$LATEST_MIGRATION_FILE" ]]

LATEST_MIGRATION_NUMBER="$((
    10#${LATEST_MIGRATION_FILE%%_*}
))"

CURRENT_MIGRATION_NUMBER="$(
    sqlite3 "$DB" 'PRAGMA user_version;'
)"

[[ "$CURRENT_MIGRATION_NUMBER" == "$LATEST_MIGRATION_NUMBER" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM sqlite_master
         WHERE type='table'
           AND name='snapshots';"
)" == "1" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM project_locks;"
)" == "0" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT enabled
         FROM projects
         WHERE project_id='${FIXTURE_ID}';"
)" == "0" ]]

[[ "$(
    git -C "$FIXTURE_REPO" branch --show-current
)" == "main" ]]

[[ -z "$(
    git -C "$FIXTURE_REPO" \
        status \
        --porcelain=v1 \
        --untracked-files=all
)" ]]

LATEST_RUN="$(
    sqlite3 "$DB" \
        "SELECT run_id
         FROM runs
         WHERE project_id='${FIXTURE_ID}'
           AND recovery_decision='ROLLBACK_SAFE'
         ORDER BY created_at DESC
         LIMIT 1;"
)"

[[ -n "$LATEST_RUN" ]]

"${REPO}/scripts/orchestra-transaction.py" \
    verify-snapshot \
    --run "$LATEST_RUN" \
    >/dev/null

git -C "$FIXTURE_REPO" fsck --no-dangling

echo "Orchestra transaction foundation: PASS"
