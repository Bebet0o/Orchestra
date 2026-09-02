#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$REPO" python3 "$REPO/tests/test_controller_objective_commands.py"
python3 - "$REPO" <<'PY'
from pathlib import Path
import ast
import sys
root = Path(sys.argv[1])
for item in (
    root / "controller_api/objective_commands.py",
    root / "controller_api/objective_command_probe.py",
    root / "scripts/orchestra-controller-objective-command-probe.py",
    root / "controller_api/server.py",
    root / "controller_api/core.py",
):
    ast.parse(item.read_text(encoding="utf-8"), filename=str(item))
text = (root / "controller_api/objective_commands.py").read_text(encoding="utf-8")
for required in (
    "BEGIN IMMEDIATE",
    "controller_idempotency",
    "controller_command_audit",
    "verify_csrf_token",
    "objective.create",
    'command not in {"pause", "resume", "cancel"}',
    'kind=f"objective.{command}"',
):
    if required not in text:
        raise SystemExit(f"Missing objective command contract marker: {required}")
probe = (root / "controller_api/objective_command_probe.py").read_text(encoding="utf-8")
for required in (
    "2099-01-01T00:00:00Z",
    "commands/pause",
    "commands/resume",
    "commands/cancel",
    "did not preserve its future schedule",
):
    if required not in probe:
        raise SystemExit(f"Missing safe live-probe marker: {required}")
print("Controller secure objective command contract: PASS")
PY
printf '%s\n' ORCHESTRA_CONTROLLER_OBJECTIVE_COMMANDS_PASS
