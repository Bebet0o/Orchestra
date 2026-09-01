#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m unittest -v \
    tests.test_sandbox_profiles \
    tests.test_controller_sandbox_profile_reads

python3 - <<'PY'
from __future__ import annotations

import ast
import json
from pathlib import Path

root = Path.cwd()
migration = root / "migrations/020_sandbox_profile_persistence.sql"
source = migration.read_text(encoding="utf-8")
for phrase in (
    "CREATE TABLE sandbox_profiles",
    "CREATE TABLE sandbox_profile_revisions",
    "sandbox profile revisions are immutable",
    "PRAGMA user_version = 20",
):
    if phrase not in source:
        raise SystemExit(f"sandbox profile migration contract missing: {phrase}")
for forbidden in (
    "create table sandbox_builds",
    "create table sandbox_build_logs",
    "docker build",
):
    if forbidden in source.lower():
        raise SystemExit(f"2O persistence foundation exceeds scope: {forbidden}")

module = root / "controller_api/sandbox_profiles.py"
tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        names = {alias.name.split(".", 1)[0] for alias in node.names}
    elif isinstance(node, ast.ImportFrom):
        names = {node.module.split(".", 1)[0]} if node.module else set()
    else:
        names = set()
    if names & {"subprocess", "socket", "urllib", "requests"}:
        raise SystemExit("sandbox profile store imports execution/network code")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"eval", "exec", "compile"}:
            raise SystemExit("sandbox profile store uses dynamic execution")

core = (root / "controller_api/core.py").read_text(encoding="utf-8")
for phrase in (
    '"sandbox_profile_reads": True',
    '"sandbox_profile_operator_import": True',
    '"sandbox_profile_http_writes": True',
    '"sandbox_profile_http_validation": True',
    '"blueprint_builds": False',
):
    if phrase not in core:
        raise SystemExit(f"Controller sandbox capability drift: {phrase}")

server = (root / "controller_api/server.py").read_text(encoding="utf-8")
for route in (
    'path == "/api/v1/sandboxes"',
    'sandbox_prefix = "/api/v1/sandboxes/"',
):
    if route not in server:
        raise SystemExit(f"Controller sandbox route missing: {route}")

openapi = json.loads(
    (root / "specs/controller-api-v1.openapi.json").read_text(encoding="utf-8")
)
list_ref = (
    openapi["paths"]["/sandboxes"]["get"]["responses"]["200"]
    ["content"]["application/json"]["schema"]["$ref"]
)
if list_ref != "#/components/schemas/SandboxListResponse":
    raise SystemExit("OpenAPI sandbox list response contract is incorrect")

state_parameters = [
    item
    for item in openapi["paths"]["/sandboxes"]["get"].get("parameters", [])
    if isinstance(item, dict) and item.get("name") == "state"
]
if len(state_parameters) != 1:
    raise SystemExit("OpenAPI sandbox state filter is absent or ambiguous")
profile_schema = openapi["components"]["schemas"]["SandboxProfile"]
properties = profile_schema.get("properties", {})
for required in (
    "profile_name",
    "source_sha256",
    "canonical_sha256",
    "canonical_size",
    "diagnostics",
):
    if required not in properties or required not in profile_schema.get("required", []):
        raise SystemExit(f"OpenAPI sandbox metadata field missing: {required}")
for forbidden in ("source", "source_text", "canonical", "canonical_json"):
    if forbidden in properties:
        raise SystemExit(f"OpenAPI exposes private sandbox field: {forbidden}")

feature_schemas = []
def collect_feature_schemas(value):
    if isinstance(value, dict):
        schema_properties = value.get("properties")
        if isinstance(schema_properties, dict):
            features = schema_properties.get("features")
            if isinstance(features, dict):
                feature_schemas.append(features)
        for item in value.values():
            collect_feature_schemas(item)
    elif isinstance(value, list):
        for item in value:
            collect_feature_schemas(item)
collect_feature_schemas(openapi.get("components", {}).get("schemas", {}))
if not feature_schemas:
    raise SystemExit("OpenAPI generic feature map schema is absent")
for schema in feature_schemas:
    additional = schema.get("additionalProperties")
    if (
        schema.get("type") != "object"
        or not isinstance(additional, dict)
        or additional.get("type") != "boolean"
    ):
        raise SystemExit("OpenAPI generic feature map schema drift")

print("Sandbox profile migration/read/capability contract: PASS")
PY

echo ORCHESTRA_SANDBOX_PROFILE_PERSISTENCE_PASS
