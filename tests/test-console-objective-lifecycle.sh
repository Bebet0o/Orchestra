#!/usr/bin/env bash
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "${REPO}/scripts/orchestra-console-build.py" check \
  --source "${REPO}/console/src" \
  --expected "${REPO}/console/dist"

NODE_BIN="$(command -v node || true)"
if [[ -n "$NODE_BIN" ]]; then
  "$NODE_BIN" --check "${REPO}/console/src/app.js"
  "$NODE_BIN" --check "${REPO}/console/src/controller-client.js"
  echo "ORCHESTRA_2U_CONSOLE_NODE_SYNTAX_PASS binary=$NODE_BIN"
else
  echo "ORCHESTRA_2U_CONSOLE_NODE_SYNTAX_SKIPPED reason=node_unavailable"
fi

cd "$REPO"
python3 -m unittest -v tests.test_console_objective_lifecycle

echo "ORCHESTRA_2U_CONSOLE_OBJECTIVE_LIFECYCLE_PASS"
