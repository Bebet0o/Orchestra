#!/usr/bin/env bash
set -Eeuo pipefail

WEBUI="orchestra-hermes-webui"

[[ "$(
    docker inspect "$WEBUI" \
        --format '{{.State.Running}}'
)" == "true" ]]

[[ "$(
    docker inspect "$WEBUI" \
        --format '{{.State.Health.Status}}'
)" == "healthy" ]]

[[ "$(docker port "$WEBUI" 8787/tcp)" == "127.0.0.1:8787" ]]

curl \
    --silent \
    --show-error \
    --fail \
    http://127.0.0.1:8787/health |
    jq -e '.status == "ok"' >/dev/null

docker inspect "$WEBUI" |
jq -e '
    .[0].Mounts |
    all(
      .Destination != "/var/run/docker.sock"
      and .Destination != "/run/orchestra-docker/docker.sock"
    )
' >/dev/null

docker inspect "$WEBUI" |
jq -e '
    .[0].Mounts |
    any(
      .Destination == "/workspace"
      and .RW == false
    )
' >/dev/null

docker inspect "$WEBUI" |
jq -e '
    .[0].Mounts |
    any(
      .Destination
      == "/home/hermeswebui/.hermes/hermes-agent"
      and .RW == false
    )
' >/dev/null

docker exec \
    --user "$(id -u):$(id -g)" \
    "$WEBUI" \
    python - <<'PY'
import json
import os
import urllib.request

assert os.environ["HERMES_WEBUI_CHAT_BACKEND"] == "gateway"
assert (
    os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"]
    == "http://hermes-agent:8642"
)

key = os.environ["HERMES_WEBUI_GATEWAY_API_KEY"]

request = urllib.request.Request(
    "http://hermes-agent:8642/health/detailed",
    headers={"Authorization": f"Bearer {key}"},
)

with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)

assert payload["status"] == "ok"
assert payload["gateway_state"] == "running"

print("WebUI gateway connectivity: PASS")
PY

echo "Hermes WebUI foundation: PASS"
