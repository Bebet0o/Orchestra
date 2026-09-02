#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$REPO" python3 "$REPO/tests/test_controller_review_commands.py"
python3 - "$REPO" <<'PY'
from pathlib import Path
import ast
import sys
root = Path(sys.argv[1])
for item in (
    root / "controller_api/review_commands.py",
    root / "controller_api/review_command_probe.py",
    root / "scripts/orchestra-controller-review-command-probe.py",
    root / "controller_api/server.py",
    root / "controller_api/core.py",
):
    ast.parse(item.read_text(encoding="utf-8"), filename=str(item))
text = (root / "controller_api/review_commands.py").read_text(encoding="utf-8")
for required in (
    "BEGIN IMMEDIATE",
    "controller_review_idempotency",
    "controller_review_command_audit",
    'SAFE_REVIEW_COMMANDS = {"acknowledge-debt", "request-human-review"}',
    'command == "rerun"',
    "REVIEW_DEBT_ACKNOWLEDGED",
    "REVIEW_HUMAN_REQUESTED",
):
    if required not in text:
        raise SystemExit(f"Missing review command contract marker: {required}")
print("Controller bounded human review command contract: PASS")
PY
printf '%s\n' ORCHESTRA_CONTROLLER_REVIEW_COMMANDS_PASS
