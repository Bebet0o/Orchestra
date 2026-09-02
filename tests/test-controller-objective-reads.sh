#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export PYTHONWARNINGS="error::ResourceWarning"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

python3 -m compileall -q \
    controller_api/objective_reads.py \
    controller_api/objective_probe.py \
    scripts/orchestra-controller-objective-probe.py \
    tests/test_controller_objective_reads.py

python3 tests/test_controller_objective_reads.py
python3 scripts/orchestra-controller-objective-probe.py --help >/dev/null
(
    cd /tmp
    env -u PYTHONPATH         python3 "$REPO/scripts/orchestra-controller-objective-probe.py"         --help >/dev/null
)

python3 - "$REPO/specs/controller-api-v1.openapi.json" <<'PY'
import json
import sys
from pathlib import Path

contract = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for path in (
    "/objectives",
    "/objectives/{objective_id}",
    "/projects/{project_id}/objectives",
    "/operations/{operation_id}",
):
    if "get" not in contract["paths"].get(path, {}):
        raise SystemExit(f"Missing OpenAPI GET contract: {path}")

for schema in ("Objective", "Operation"):
    if schema not in contract["components"]["schemas"]:
        raise SystemExit(f"Missing OpenAPI schema: {schema}")

print("Controller objective/operation OpenAPI subset: PASS")
PY

python3 - "$REPO/controller_api/objective_reads.py" "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

store = Path(sys.argv[1]).read_text(encoding="utf-8")
server = Path(sys.argv[2]).read_text(encoding="utf-8")
for marker in (
    "ReadOnlyDatabase",
    "legacy_payload_redacted",
    "MAX_CURSOR_BYTES",
    "json_valid(q.project_scope_json)",
    "hmac.compare_digest",
    "latest.attempt_number DESC",
    "2**53",
):
    if marker not in store:
        raise SystemExit(f"Objective read hardening marker missing: {marker}")
for forbidden in (
    "INSERT INTO objective_queue",
    "UPDATE objective_queue",
    "DELETE FROM objective_queue",
    '"last_error":',
    '"failure_reason":',
    '"result_json":',
):
    if forbidden in store:
        raise SystemExit(f"Unsafe objective projection marker: {forbidden}")
for route in (
    'path == "/api/v1/objectives"',
    '"/api/v1/projects/"',
    '"/api/v1/objectives/"',
    '"/api/v1/operations/"',
):
    if route not in server:
        raise SystemExit(f"Objective route missing: {route}")
print("Controller objective/operation read-only contract: PASS")
PY

python3 - "$REPO/tests/test_controller_service.py" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    "CREATE TABLE orchestration_plans",
    "CREATE TABLE objective_queue",
    "CREATE TABLE objective_attempts",
    "CREATE TABLE objective_events",
    "test_probe_fixture_satisfies_objective_readiness_schema",
):
    if marker not in text:
        raise SystemExit(
            f"Durable-service readiness regression marker missing: {marker}"
        )
print("Controller durable-service readiness fixture: PASS")
PY

python3 - "$REPO/scripts/orchestra-controller-objective-probe.py" <<'PY'
from pathlib import Path
import sys

script = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    'REPO = Path(__file__).resolve().parents[1]',
    'sys.path.insert(0, str(REPO))',
):
    if marker not in script:
        raise SystemExit(
            f"Objective probe standalone bootstrap missing: {marker}"
        )
print("Controller objective probe standalone bootstrap: PASS")
PY

python3 - "$REPO/controller_api/core.py" "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
server = Path(sys.argv[2]).read_text(encoding="utf-8")

if "def authenticate(self, cookie_header: str | None) -> str:" not in core:
    raise SystemExit("Authentication must return the validated cursor secret.")
if "cursor_secret=session_token" not in server:
    raise SystemExit("Objective cursor signing secret is not wired to routes.")

print("Controller objective cursor authentication binding: PASS")
PY

echo "ORCHESTRA_CONTROLLER_OBJECTIVE_READS_PASS"
