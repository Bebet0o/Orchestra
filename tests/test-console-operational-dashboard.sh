#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$REPO/scripts/orchestra-console-build.py" check \
  --source "$REPO/console/src" \
  --expected "$REPO/console/dist"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_console_operational_dashboard

echo "ORCHESTRA_CONSOLE_OPERATIONAL_DASHBOARD_PASS"
