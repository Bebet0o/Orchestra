#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"

cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT

TEST_ROOT="${TMP}/orchestra"
TEST_REPO="${TEST_ROOT}/repo"

mkdir -p \
    "${TEST_REPO}/config/projects.d" \
    "${TEST_REPO}/migrations" \
    "${TEST_REPO}/scripts" \
    "${TEST_ROOT}/state/controller" \
    "${TEST_ROOT}/workspaces" \
    "${TEST_ROOT}/project-data" \
    "${TEST_ROOT}/backups" \
    "${TEST_ROOT}/runtime" \
    "${TEST_ROOT}/secrets"

rsync -a \
    --exclude='projects.d/*.toml' \
    "${REPO}/config/" \
    "${TEST_REPO}/config/"

cp -a "${REPO}/migrations/." "${TEST_REPO}/migrations/"
cp -a \
    "${REPO}/scripts/orchestra-db.py" \
    "${REPO}/scripts/orchestra-registry.py" \
    "${TEST_REPO}/scripts/"

if find "${TEST_REPO}/config/projects.d" \
    -maxdepth 1 \
    -type f \
    -name '*.toml' |
    grep -q .
then
    echo "Le dépôt public simulé contient un projet actif." >&2
    exit 1
fi

ORCHESTRA_ROOT="$TEST_ROOT" \
    "${TEST_REPO}/scripts/orchestra-db.py" migrate >/dev/null

ORCHESTRA_ROOT="$TEST_ROOT" \
    "${TEST_REPO}/scripts/orchestra-registry.py" validate >/dev/null

ORCHESTRA_ROOT="$TEST_ROOT" \
    "${TEST_REPO}/scripts/orchestra-registry.py" sync >/dev/null

project_count="$(
    sqlite3 \
        "${TEST_ROOT}/state/controller/orchestra.db" \
        'SELECT COUNT(*) FROM projects;'
)"

enabled_count="$(
    sqlite3 \
        "${TEST_ROOT}/state/controller/orchestra.db" \
        'SELECT COUNT(*) FROM projects WHERE enabled = 1;'
)"

[[ "$project_count" == "0" ]]
[[ "$enabled_count" == "0" ]]

sqlite3 "${TEST_ROOT}/state/controller/orchestra.db" \
    'PRAGMA quick_check;' |
    grep -Fxq ok

[[ -z "$(
    sqlite3 "${TEST_ROOT}/state/controller/orchestra.db" \
        'PRAGMA foreign_key_check;'
)" ]]

echo "Orchestra public empty registry: PASS"
