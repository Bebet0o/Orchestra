#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 scripts/hermesops-console-build.py check \
  --source console/src \
  --expected console/dist

NODE_BIN=""
if command -v node >/dev/null 2>&1; then
  NODE_BIN="$(command -v node)"
elif command -v nodejs >/dev/null 2>&1; then
  NODE_BIN="$(command -v nodejs)"
fi

if [[ -n "$NODE_BIN" ]]; then
  "$NODE_BIN" --check console/src/app.js
  "$NODE_BIN" --check console/src/controller-client.js
  echo "HERMESOPS_2T_CONSOLE_NODE_SYNTAX_PASS binary=$NODE_BIN"
else
  echo "HERMESOPS_2T_CONSOLE_NODE_SYNTAX_SKIPPED reason=node_unavailable"
fi

python3 -m unittest -v tests.test_console_blueprint_lifecycle

echo "HERMESOPS_3B_CONSOLE_BLUEPRINT_LIFECYCLE_PASS"
