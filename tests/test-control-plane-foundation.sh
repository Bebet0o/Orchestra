#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"

"${REPO}/scripts/orchestra-registry.py" validate
"${REPO}/scripts/orchestra-db.py" migrate
"${REPO}/scripts/orchestra-registry.py" sync
"${REPO}/scripts/orchestra-db.py" integrity

[[ -f "$DB" ]]
[[ "$(stat -c '%a' "$DB")" == "640" ]]

[[ "$(
    sqlite3 "$DB" 'PRAGMA journal_mode;'
)" == "wal" ]]

LATEST_MIGRATION_RAW="$(
    find "${REPO}/migrations" \
        -maxdepth 1 \
        -type f \
        -name '[0-9][0-9][0-9]_*.sql' \
        -printf '%f\n' |
    sed -nE 's/^([0-9]{3})_.*/\1/p' |
    sort -n |
    tail -n 1
)"

[[ -n "$LATEST_MIGRATION_RAW" ]] || {
    echo "Aucune migration trouvée." >&2
    exit 1
}

LATEST_MIGRATION="$((10#${LATEST_MIGRATION_RAW}))"

DATABASE_VERSION="$(
    sqlite3 "$DB" 'PRAGMA user_version;'
)"

[[ "$DATABASE_VERSION" == "$LATEST_MIGRATION" ]] || {
    echo \
      "Version SQLite inattendue : " \
      "${DATABASE_VERSION}, attendue ${LATEST_MIGRATION}" \
      >&2
    exit 1
}

while IFS= read -r migration_file; do
    raw_version="$(
        basename "$migration_file" |
        sed -nE 's/^([0-9]{3})_.*/\1/p'
    )"

    version="$((10#${raw_version}))"

    applied="$(
        sqlite3 "$DB" \
            "SELECT COUNT(*)
             FROM schema_migrations
             WHERE version=${version};"
    )"

    [[ "$applied" == "1" ]] || {
        echo \
          "Migration non enregistrée : " \
          "$migration_file" \
          >&2
        exit 1
    }
done < <(
    find "${REPO}/migrations" \
        -maxdepth 1 \
        -type f \
        -name '[0-9][0-9][0-9]_*.sql' |
    sort
)

required_tables=(
    projects
    roles
    runs
    tasks
    project_locks
    snapshots
    worker_executions
    events
    approvals
    review_results
    memory_records
)

for table in "${required_tables[@]}"; do
    count="$(
        sqlite3 "$DB" \
            "SELECT COUNT(*)
             FROM sqlite_master
             WHERE type='table'
               AND name='${table}';"
    )"

    [[ "$count" == "1" ]] || {
        echo "Table absente : $table" >&2
        exit 1
    }
done

echo "Migration courante : ${DATABASE_VERSION}"
echo "Orchestra control-plane foundation: PASS"
