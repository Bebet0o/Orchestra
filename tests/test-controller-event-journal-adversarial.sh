#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest -v tests.test_controller_event_journal_adversarial
printf 'ORCHESTRA_CONTROLLER_EVENT_JOURNAL_ADVERSARIAL_PASS\n'
