#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
AGENT="orchestra-hermes-agent"

echo "=== Modèle configuré ==="

docker exec \
    --user hermes \
    "$AGENT" \
    hermes config get model.provider

docker exec \
    --user hermes \
    "$AGENT" \
    hermes config get model.default

echo
echo "=== OAuth Codex ==="

docker exec \
    -i \
    --user hermes \
    "$AGENT" \
    python - <<'PY'
import json
import os
from pathlib import Path
from typing import Any

path = Path(os.environ["HERMES_HOME"]) / "auth.json"

if not path.exists():
    raise SystemExit("auth.json absent")


def contains_token(value: Any) -> bool:
    if isinstance(value, dict):
        token = value.get("access_token")
        if isinstance(token, str) and len(token.strip()) >= 20:
            return True
        return any(contains_token(item) for item in value.values())

    if isinstance(value, list):
        return any(contains_token(item) for item in value)

    return False


data = json.loads(path.read_text())
nodes = []

for section in ("providers", "credential_pool"):
    value = data.get(section)
    if isinstance(value, dict):
        nodes.append(value.get("openai-codex"))

print(
    "OpenAI Codex credential: PRESENT"
    if any(contains_token(node) for node in nodes)
    else "OpenAI Codex credential: ABSENT"
)
PY

echo
echo "=== Gateway ==="

curl \
    --silent \
    --show-error \
    --fail \
    http://127.0.0.1:8642/health |
    jq .
