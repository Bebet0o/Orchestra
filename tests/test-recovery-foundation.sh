#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"
FIXTURE_REPO="${ROOT}/workspaces/.fixtures/transaction-fixture"
PRIVATE_DOCKER_HOST="unix://${ROOT}/runtime/sandbox-engine-socket/docker.sock"

[[ -x "${REPO}/scripts/orchestra-recovery.py" ]]
[[ -f "${REPO}/migrations/007_recovery_manager.sql" ]]
[[ -f "${REPO}/docs/RECOVERY.md" ]]

python3 -m py_compile \
    "${REPO}/scripts/orchestra-recovery.py"

"${REPO}/scripts/orchestra-recovery.py" self-test

grep -Fq 'def assess_run' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'def cleanup_run_resources' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'def cleanup_orphans' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'def prune_empty_clone_parents' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'prune_empty_clone_parents(path.parent, root)' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'RECOVERY_INTEGRATION_COMPLETED' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'snapshot-integrity-failed' \
    "${REPO}/scripts/orchestra-recovery.py"
grep -Fq 'default-branch-diverged' \
    "${REPO}/scripts/orchestra-recovery.py"

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
           AND name='recovery_executions';"
)" == "1" ]]

[[ "$(
    sqlite3 "$DB" \
        "SELECT role_kind || '|' || workspace_mode || '|' || may_push
         FROM roles
         WHERE role_id='recovery';"
)" == "recovery|controller_only|0" ]]

for pair in \
    'RESUME_SAFE|RESUMED' \
    'ROLLBACK_SAFE|ROLLED_BACK' \
    'BLOCK_HUMAN|BLOCKED'
do
    decision="${pair%%|*}"
    outcome="${pair##*|}"
    [[ "$(
        sqlite3 "$DB" \
            "SELECT COUNT(*)
             FROM recovery_executions
             WHERE decision='${decision}'
               AND outcome='${outcome}';"
    )" -ge 1 ]]
done

[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM recovery_executions
         WHERE policy_version <> 'recovery-policy-v1'
            OR source_profile <> 'ops-recovery'
            OR role_id <> 'recovery'
            OR length(evidence_sha256) <> 64
            OR evidence_json = ''
            OR actions_json = '';"
)" == "0" ]]

sqlite3 "$DB" 'PRAGMA foreign_key_check;' |
grep -q . && {
    echo "SQLite foreign_key_check failed." >&2
    exit 1
}
[[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" == "ok" ]]

[[ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM project_locks;')" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM projects
         WHERE enabled=1
           AND project_id IN (
               'transaction-fixture',
               'transaction-fixture-b'
           );"
)" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM runs
         WHERE status IN (
             'SNAPSHOTTING',
             'RUNNING',
             'REVIEWING',
             'WAITING_HUMAN',
             'COMMITTING',
             'RECOVERING'
         );"
)" == "0" ]]
[[ "$(
    sqlite3 "$DB" \
        "SELECT COUNT(*)
         FROM approvals
         WHERE status='PENDING';"
)" == "0" ]]

[[ "$(git -C "$FIXTURE_REPO" branch --show-current)" == "main" ]]
[[ -z "$(
    git -C "$FIXTURE_REPO" \
        status --porcelain=v1 --untracked-files=all
)" ]]

if docker --host "$PRIVATE_DOCKER_HOST" ps -aq \
    --filter 'label=orchestra-runtime-container=1' |
    grep -q .
then
    echo "Conteneur runtime Orchestra résiduel." >&2
    exit 1
fi

if docker --host "$PRIVATE_DOCKER_HOST" ps -aq \
    --filter 'label=orchestra-sandbox=1' |
    grep -q .
then
    echo "Sandbox recovery résiduelle." >&2
    exit 1
fi

if find "${ROOT}/state/hermes-home/profiles" \
    -mindepth 1 -maxdepth 1 -type d \
    \( -name 'runtime-worker-recovery-*' \
       -o -name 'runtime-reviewer-recovery-*' \) \
    -print -quit |
    grep -q .
then
    echo "Profil runtime recovery résiduel." >&2
    exit 1
fi

if find "${ROOT}/workspaces/.orchestra-worker-clones" \
    -mindepth 1 -print -quit 2>/dev/null |
    grep -q .
then
    echo "Clone worker résiduel." >&2
    exit 1
fi

if find "${ROOT}/workspaces/.orchestra-reviewer-clones" \
    -mindepth 1 -print -quit 2>/dev/null |
    grep -q .
then
    echo "Clone reviewer résiduel." >&2
    exit 1
fi

echo "Orchestra deterministic recovery foundation: PASS"
