#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
DB="${ROOT}/state/controller/orchestra.db"

[[ -x "${REPO}/scripts/orchestra-notifier.py" ]]
[[ -x "${REPO}/scripts/orchestra-control.py" ]]
[[ -x "${REPO}/scripts/orchestractl" ]]
[[ -x "${REPO}/scripts/configure-orchestra-telegram.sh" ]]
[[ -f "${REPO}/migrations/011_notification_outbox.sql" ]]
[[ -f "${REPO}/config/notifier.toml" ]]
[[ -f "${REPO}/compose/orchestra.yaml" ]]
[[ -f "${REPO}/docs/NOTIFICATIONS.md" ]]

[[ "$(sqlite3 "$DB" 'PRAGMA user_version;')" == "11" ]]
[[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" == "ok" ]]
! sqlite3 "$DB" 'PRAGMA foreign_key_check;' | grep -q .

for table in \
    notifier_instances \
    notification_outbox \
    notification_deliveries \
    notification_cursors
do
    [[ "$(
        sqlite3 "$DB" \
            "SELECT COUNT(*) FROM sqlite_master
             WHERE type='table' AND name='${table}';"
    )" == "1" ]]
done

"${REPO}/scripts/orchestra-compose.sh" ps --status running --services |
    grep -Fxq notifier

STATUS_JSON="$("${REPO}/scripts/orchestra-notifier.py" status)"
python3 - "$STATUS_JSON" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["version"] == "notifier-v1"
assert payload["lock_held"] is True
assert payload["instance"]["status"] == "RUNNING"
assert payload["outbox_counts"].get("DEAD_LETTER", 0) == 0
PY

"${REPO}/scripts/orchestra-notifier.py" self-test
"${REPO}/scripts/orchestra-control.py" self-test
"${REPO}/scripts/orchestra-control.py" queue --json >/dev/null
"${REPO}/scripts/orchestra-control.py" approvals --all --json >/dev/null

notification_foundation_ready() {
    [[ -s "${ROOT}/runtime/notifications/delivered.jsonl" ]] || return 1

    [[ "$(
        sqlite3 "$DB"             "SELECT COUNT(*)
             FROM notification_outbox
             WHERE channel='FILE'
               AND status='DELIVERED';"
    )" -ge 1 ]] || return 1

    [[ "$(
        sqlite3 "$DB"             "SELECT COUNT(*)
             FROM notification_outbox
             WHERE subject_type='OBJECTIVE'
               AND status='DELIVERED';"
    )" -ge 1 ]] || return 1

    [[ "$(
        sqlite3 "$DB"             "SELECT COUNT(*)
             FROM notification_outbox
             WHERE subject_type='APPROVAL'
               AND status='DELIVERED';"
    )" -ge 1 ]] || return 1

    [[ "$(
        sqlite3 "$DB"             "SELECT COUNT(*)
             FROM notification_outbox
             WHERE status IN (
                 'PENDING',
                 'DELIVERING',
                 'RETRY',
                 'DEAD_LETTER'
             );"
    )" == "0" ]]
}

NOTIFICATION_FOUNDATION_READY=0
for _ in $(seq 1 180)
do
    if notification_foundation_ready; then
        NOTIFICATION_FOUNDATION_READY=1
        break
    fi
    sleep 1
done

if [[ "$NOTIFICATION_FOUNDATION_READY" != "1" ]]; then
    "${REPO}/scripts/orchestra-notifier.py" status >&2 || true
    "${REPO}/scripts/orchestra-notifier.py" list --limit 200 >&2 || true
    echo "Notification foundation did not reach a drained state." >&2
    exit 1
fi

grep -Fq 'processes["notifier"] = spawn' "${REPO}/scripts/orchestra-appliance.py"
grep -Fq 'no-new-privileges:true' "${REPO}/compose/orchestra.yaml"

echo "Orchestra durable operator notification foundation: PASS"
