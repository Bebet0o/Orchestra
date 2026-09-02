#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export PYTHONWARNINGS="error::ResourceWarning"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

python3 -m compileall -q \
    controller_api/execution_reads.py \
    controller_api/execution_probe.py \
    scripts/orchestra-controller-execution-probe.py \
    tests/test_controller_execution_reads.py

python3 tests/test_controller_execution_reads.py
python3 scripts/orchestra-controller-execution-probe.py --help >/dev/null
(
    cd /tmp
    env -u PYTHONPATH \
        python3 "$REPO/scripts/orchestra-controller-execution-probe.py" \
        --help >/dev/null
)

python3 - "$REPO/specs/controller-api-v1.openapi.json" <<'PY'
import json
import sys
from pathlib import Path

contract = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for path in (
    "/objectives/{objective_id}/tasks",
    "/tasks/{task_id}",
    "/tasks/{task_id}/runs",
    "/runs/{run_id}",
    "/runs/{run_id}/logs",
):
    if "get" not in contract["paths"].get(path, {}):
        raise SystemExit(f"Missing OpenAPI GET contract: {path}")
for schema in (
    "Task",
    "Run",
    "LogChunk",
    "TaskListResponse",
    "RunListResponse",
    "LogChunkResponse",
):
    if schema not in contract["components"]["schemas"]:
        raise SystemExit(f"Missing OpenAPI schema: {schema}")
print("Controller task/run/log OpenAPI subset: PASS")
PY

python3 - \
    "$REPO/controller_api/execution_reads.py" \
    "$REPO/controller_api/core.py" \
    "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

store = Path(sys.argv[1]).read_text(encoding="utf-8")
core = Path(sys.argv[2]).read_text(encoding="utf-8")
server = Path(sys.argv[3]).read_text(encoding="utf-8")

for marker in (
    "ReadOnlyDatabase",
    "hmac.compare_digest",
    'ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")',
    "registered_role_id",
    "registered_workspace_mode",
    "transaction_project_id",
    "WORKSPACE_MODES",
    "def _integer(",
    "def _timestamp(",
    "event_project_id",
    "TRANSACTION_REFERENCE_PATTERN",
    "MAX_INTERNAL_TRANSACTION_KEY_BYTES",
    "joined_legacy_run_id",
    "worker_legacy_run_id",
    "hermesops-transaction-reference-v1",
    "orchestra-task-cursor-v1",
    "orchestra-run-cursor-v1",
    "legacy_payload_redacted",
    "SELECT MAX(event_id)",
    "ORDER BY a.attempt_number DESC",
    "payload_redacted",
    "2**53",
):
    if marker not in store:
        raise SystemExit(f"Execution-read hardening marker missing: {marker}")
for forbidden in (
    "INSERT INTO orchestration_",
    "UPDATE orchestration_",
    "DELETE FROM orchestration_",
    "prompt_path",
    "output_path",
    "outer_container_name",
    "sandbox_container_id",
):
    if forbidden in store:
        raise SystemExit(f"Unsafe execution-read marker: {forbidden}")
for marker in (
    '"task_reads": True',
    '"run_reads": True',
    '"worker_execution_reads": True',
    '"persisted_event_log_reads": True',
    '"raw_worker_log_reads": False',
):
    if marker not in core:
        raise SystemExit(f"Execution capability marker missing: {marker}")
for route in (
    'objective_tasks_suffix = "/tasks"',
    'task_runs_suffix = "/runs"',
    'run_logs_suffix = "/logs"',
    'service.executions.get_task',
    'service.executions.get_run',
    'service.executions.get_run_logs',
):
    if route not in server:
        raise SystemExit(f"Execution route marker missing: {route}")
print("Controller task/run/worker/event-log read-only contract: PASS")
PY

python3 - "$REPO/tests/test_controller_service.py" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    "CREATE TABLE roles",
    "CREATE TABLE runs",
    "CREATE TABLE events",
    "CREATE TABLE worker_executions",
    "CREATE TABLE orchestration_tasks",
    "CREATE TABLE orchestration_attempts",
    "CREATE TABLE orchestration_dependencies",
):
    if marker not in text:
        raise SystemExit(f"Execution readiness fixture missing: {marker}")
print("Controller execution readiness fixture: PASS")
PY

python3 - "$REPO/scripts/orchestra-controller-execution-probe.py" <<'PY'
from pathlib import Path
import sys

script = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    'REPO = Path(__file__).resolve().parents[1]',
    'sys.path.insert(0, str(REPO))',
):
    if marker not in script:
        raise SystemExit(f"Execution probe standalone bootstrap missing: {marker}")
print("Controller execution probe standalone bootstrap: PASS")
PY

python3 - "$REPO/controller_api/core.py" "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
server = Path(sys.argv[2]).read_text(encoding="utf-8")
if "def authenticate(self, cookie_header: str | None) -> str:" not in core:
    raise SystemExit("Authentication must return the validated cursor secret.")
if server.count("cursor_secret=session_token") < 3:
    raise SystemExit("Signed execution cursors are not bound to authentication.")
print("Controller execution cursor authentication binding: PASS")
PY

echo "ORCHESTRA_CONTROLLER_EXECUTION_READS_PASS"
