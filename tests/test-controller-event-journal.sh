#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
PYTHONPATH="$REPO" python3 -m unittest -v tests/test_controller_event_journal.py
echo ORCHESTRA_CONTROLLER_EVENT_JOURNAL_PASS
