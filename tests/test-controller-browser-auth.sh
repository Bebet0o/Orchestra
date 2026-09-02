#!/usr/bin/env bash
set -Eeuo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH="$REPO" python3 -m unittest -v tests.test_controller_browser_auth
echo "ORCHESTRA_CONTROLLER_BROWSER_AUTH_PASS"
