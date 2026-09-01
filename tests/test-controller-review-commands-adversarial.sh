#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/tests"
PYTHONPATH="$ROOT:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m unittest -v test_controller_review_commands_adversarial
printf '%s\n' 'ORCHESTRA_CONTROLLER_REVIEW_COMMANDS_ADVERSARIAL_PASS'
