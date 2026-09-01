#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_console_project_lifecycle
echo "ORCHESTRA_CONSOLE_PROJECT_LIFECYCLE_PASS"
