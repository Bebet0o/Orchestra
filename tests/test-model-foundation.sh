#!/usr/bin/env bash
set -Eeuo pipefail

AGENT="orchestra-hermes-agent"

[[ "$(
    docker inspect "$AGENT" \
        --format '{{.State.Health.Status}}'
)" == "healthy" ]]

docker exec \
    -i \
    --user hermes \
    "$AGENT" \
    python - <<'PY'
import json
import os
from pathlib import Path
from typing import Any

import yaml

home = Path(os.environ["HERMES_HOME"])

config = yaml.safe_load(
    (home / "config.yaml").read_text()
) or {}

model = config.get("model") or {}

assert model.get("provider") == "openai-codex"
assert model.get("default") == "gpt-5.6-sol"


def contains_token(value: Any) -> bool:
    if isinstance(value, dict):
        token = value.get("access_token")
        if isinstance(token, str) and len(token.strip()) >= 20:
            return True
        return any(contains_token(item) for item in value.values())

    if isinstance(value, list):
        return any(contains_token(item) for item in value)

    return False


auth = json.loads((home / "auth.json").read_text())
nodes = []

for section in ("providers", "credential_pool"):
    value = auth.get(section)
    if isinstance(value, dict):
        nodes.append(value.get("openai-codex"))

assert any(contains_token(node) for node in nodes)

print("Model and Codex OAuth config: PASS")
PY

curl \
    --silent \
    --show-error \
    --fail \
    http://127.0.0.1:8642/health |
    jq -e '.status == "ok"' >/dev/null

echo "Hermes model foundation: PASS"
