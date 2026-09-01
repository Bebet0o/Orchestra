#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"

"${REPO}/scripts/orchestra-roles.py" validate
"${REPO}/scripts/orchestra-db.py" migrate
"${REPO}/scripts/orchestra-roles.py" sync
"${REPO}/scripts/orchestra-roles.py" verify-profiles
"${REPO}/scripts/orchestra-db.py" integrity

ROLE_COUNT="$(
    sqlite3 "$DB" \
        'SELECT COUNT(*) FROM roles WHERE enabled = 1;'
)"

[[ "$ROLE_COUNT" == "6" ]] || {
    echo "Nombre de rôles inattendu : $ROLE_COUNT" >&2
    exit 1
}

PUSH_COUNT="$(
    sqlite3 "$DB" \
        'SELECT COUNT(*) FROM roles WHERE may_push != 0;'
)"

[[ "$PUSH_COUNT" == "0" ]]

echo "Orchestra role fleet: PASS"
