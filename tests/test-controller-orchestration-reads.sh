#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m unittest -v tests.test_controller_orchestration_reads

python3 - <<'PY'
from __future__ import annotations
import json
from pathlib import Path

root = Path.cwd()
openapi = json.loads((root / "specs/controller-api-v1.openapi.json").read_text(encoding="utf-8"))
required = (
    "/plans",
    "/plans/{plan_id}",
    "/plans/{plan_id}/tasks",
    "/plans/{plan_id}/dependencies",
    "/plans/{plan_id}/attempts",
    "/reviewer-assignments",
    "/reviewer-assignments/{assignment_id}",
    "/runs/{run_id}/reviewer-assignments",
)
for path in required:
    if path not in openapi.get("paths", {}) or "get" not in openapi["paths"][path]:
        raise SystemExit(f"Missing OpenAPI GET contract: {path}")

core = (root / "controller_api/core.py").read_text(encoding="utf-8")
for marker in (
    '"orchestration_plan_reads": True',
    '"orchestration_graph_reads": True',
    '"orchestration_attempt_reads": True',
    '"reviewer_assignment_reads": True',
    '"raw_orchestration_payload_reads": False',
):
    if marker not in core:
        raise SystemExit(f"Missing capability marker: {marker}")

server = (root / "controller_api/server.py").read_text(encoding="utf-8")
for marker in (
    'path == "/api/v1/plans"',
    'plan_prefix = "/api/v1/plans/"',
    'path == "/api/v1/reviewer-assignments"',
    'assignment_prefix = "/api/v1/reviewer-assignments/"',
    'nested_assignment_suffix = "/reviewer-assignments"',
):
    if marker not in server:
        raise SystemExit(f"Missing Controller route marker: {marker}")

module = (root / "controller_api/orchestration_reads.py").read_text(encoding="utf-8")
for forbidden in (
    '"instruction":',
    '"plan_json":',
    '"result_json":',
    '"failure_reason":',
    '"last_error":',
    '"assigned_by":',
    '"claim_owner":',
    '"executor_instance_id":',
    '"worktree_path":',
    '"prompt_path":',
    '"output_path":',
):
    if forbidden in module:
        raise SystemExit(f"Forbidden public payload key in orchestration reads: {forbidden}")

contract = (root / "docs/api/CONTROLLER_API_V1.md").read_text(encoding="utf-8")
for route in required:
    if f"GET {route}" not in contract:
        raise SystemExit(f"Human API contract missing route: GET {route}")
PY

echo ORCHESTRA_CONTROLLER_ORCHESTRATION_READS_PASS
