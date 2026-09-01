#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m unittest -v tests.test_blueprint_v1

python3 - <<'PY'
from __future__ import annotations

import ast
import json
from pathlib import Path

root = Path.cwd()
schema = json.loads(
    (root / "specs/blueprint-v1.schema.json").read_text(encoding="utf-8")
)
if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
    raise SystemExit("Blueprint v1 must use JSON Schema 2020-12")
if schema["properties"]["apiVersion"].get("const") != "hermesops.dev/v1":
    raise SystemExit("Blueprint v1 apiVersion contract drift")
if schema["properties"]["kind"].get("const") != "SandboxProfile":
    raise SystemExit("Blueprint v1 kind contract drift")
security = schema["properties"]["spec"]["properties"]["security"]["properties"]
for field, expected in (
    ("privileged", False),
    ("noNewPrivileges", True),
    ("secrets", False),
    ("allowDockerSocket", False),
    ("allowDeviceAccess", False),
):
    if security[field].get("const") is not expected:
        raise SystemExit(f"Blueprint security invariant drift: {field}")

source = (root / "controller_api/blueprint.py").read_text(encoding="utf-8")
tree = ast.parse(source, filename="controller_api/blueprint.py")
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".", 1)[0] for alias in node.names]
        elif node.module:
            names = [node.module.split(".", 1)[0]]
        if any(name in {"subprocess", "socket", "urllib"} for name in names):
            raise SystemExit("Blueprint validator imports an execution/network module")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"eval", "exec", "compile"}:
            raise SystemExit("Blueprint validator uses dynamic code execution")

core = (root / "controller_api/core.py").read_text(encoding="utf-8")
if '"blueprint_versions": ["v1"]' not in core:
    raise SystemExit("Controller capabilities do not advertise Blueprint v1")
if '"blueprint_builds": False' not in core:
    raise SystemExit("Controller must not claim Blueprint builds in milestone 2N")

contract = (root / "docs/blueprint/SPECIFICATION_V1.md").read_text(encoding="utf-8")
if (root / "docs/hermesfile/SPECIFICATION_V1.md").exists():
    raise SystemExit("Current Blueprint v1 specification remains at the legacy path")
for phrase in (
    "Orchestra Blueprint v1 Specification",
    "../../specs/blueprint-v1.schema.json",
    "scripts/orchestra-blueprint.py",
    "The conventional source name is",
    "not a project configuration",
    "does not contain secret values",
    "canonical SHA-256",
    "does not build or activate images",
):
    if phrase not in contract:
        raise SystemExit(f"Blueprint v1 contract phrase missing: {phrase}")

openapi = json.loads(
    (root / "specs/controller-api-v1.openapi.json").read_text(encoding="utf-8")
)
serialized = json.dumps(openapi, sort_keys=True)
if '"blueprint-v0"' in serialized or '"v0alpha1"' in serialized:
    raise SystemExit("OpenAPI still advertises Blueprint v0")
if '"blueprint-v1"' not in serialized:
    raise SystemExit("OpenAPI does not accept Blueprint v1 sources")

version_schemas = []
def collect_version_schemas(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "blueprint_versions" and isinstance(item, dict):
                version_schemas.append(item)
            collect_version_schemas(item)
    elif isinstance(value, list):
        for item in value:
            collect_version_schemas(item)

collect_version_schemas(openapi)
if not version_schemas:
    raise SystemExit("OpenAPI has no Blueprint version capability schema")
for version_schema in version_schemas:
    if version_schema.get("type") != "array":
        raise SystemExit("OpenAPI Blueprint versions must be an array")
    if version_schema.get("minItems") != 1:
        raise SystemExit("OpenAPI Blueprint versions must be non-empty")
    if version_schema.get("uniqueItems") is not True:
        raise SystemExit("OpenAPI Blueprint versions must be unique")
    items = version_schema.get("items")
    if not isinstance(items, dict) or items.get("enum") != ["v1"]:
        raise SystemExit("OpenAPI does not advertise only Blueprint v1")

print("Blueprint v1 schema/code/capability contract: PASS")
PY

python3 scripts/orchestra-blueprint.py \
    validate config/examples/Blueprint

python3 scripts/orchestra-blueprint.py \
    fingerprint config/examples/Blueprint --json \
    | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["source_format"] == "blueprint-v1"
assert payload["api_version"] == "hermesops.dev/v1"
assert len(payload["source_sha256"]) == 64
assert len(payload["canonical_sha256"]) == 64
print("Blueprint v1 CLI fingerprint: PASS")
'

echo ORCHESTRA_BLUEPRINT_V1_PASS
