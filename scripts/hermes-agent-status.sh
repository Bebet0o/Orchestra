#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
SECRET_FILE="${ROOT}/secrets/agent.env"

[[ -r "$SECRET_FILE" ]] || {
    echo "Secret absent ou illisible : $SECRET_FILE" >&2
    exit 1
}

API_KEY="$(
    sed -n 's/^API_SERVER_KEY=//p' "$SECRET_FILE" |
    head -n 1
)"

echo "=== Container ==="
docker inspect orchestra-hermes-agent \
    --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}'

echo
echo "=== Public health ==="
curl \
    --silent \
    --show-error \
    --fail \
    http://127.0.0.1:8642/health |
    jq .

echo
echo "=== Detailed readiness ==="
curl \
    --silent \
    --show-error \
    --fail \
    --header "Authorization: Bearer ${API_KEY}" \
    http://127.0.0.1:8642/health/detailed |
    jq .
