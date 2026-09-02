#!/usr/bin/env bash
set -Eeuo pipefail

WEBUI="orchestra-hermes-webui"
AGENT="orchestra-hermes-agent"

echo "=== Services ==="

docker ps \
    --filter "name=^/${WEBUI}$" \
    --filter "name=^/${AGENT}$" \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo
echo "=== WebUI health ==="

curl \
    --silent \
    --show-error \
    --fail \
    http://127.0.0.1:8787/health |
    jq .

echo
echo "=== WebUI → Gateway ==="

docker exec \
    --user "${ORCHESTRA_UID:-$(id -u)}:${ORCHESTRA_GID:-$(id -g)}" \
    "$WEBUI" \
    python - <<'PY'
import json
import os
import urllib.request

base = os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"].rstrip("/")
key = os.environ["HERMES_WEBUI_GATEWAY_API_KEY"]

request = urllib.request.Request(
    base + "/health/detailed",
    headers={"Authorization": f"Bearer {key}"},
)

with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)

print(json.dumps({
    "status": payload.get("status"),
    "gateway_state": payload.get("gateway_state"),
    "active_agents": payload.get("active_agents"),
}, indent=2))
PY
