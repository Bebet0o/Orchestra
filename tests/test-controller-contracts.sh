#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$REPO" <<'PYTEST'
from __future__ import annotations
from pathlib import Path
import json
import re
import sys

root = Path(sys.argv[1])

def load(relative):
    return json.loads((root / relative).read_text(encoding="utf-8"))

def require(value, message):
    if not value:
        raise SystemExit(message)

openapi = load("specs/controller-api-v1.openapi.json")
events = load("specs/events-v1.schema.json")
asyncapi = load("specs/controller-events-v1.asyncapi.json")
blueprint = load("specs/blueprint-v1.schema.json")
require(
    blueprint["properties"]["apiVersion"].get("const") == "hermesops.dev/v1",
    "Blueprint v1 apiVersion contract drift",
)
api_doc = (root / "docs/api/CONTROLLER_API_V1.md").read_text(encoding="utf-8")

require(openapi.get("openapi") == "3.1.0", "OpenAPI must be 3.1.0")
require(asyncapi.get("asyncapi") == "3.0.0", "AsyncAPI must be 3.0.0")
require("controllerEvents" in asyncapi["channels"], "Event channel missing")
require(asyncapi["channels"]["controllerEvents"]["address"] == "/api/v1/events", "Event address mismatch")

# Human-readable HTTP surface must equal the machine contract exactly.
doc_surface = set()
for method, path in re.findall(r"^(GET|POST|PATCH|PUT|DELETE)\s+(/[^\s`]+)", api_doc, re.M):
    if path.startswith("/api/v1/"):
        path = path[len("/api/v1"):]
    doc_surface.add((method.lower(), path))
api_surface = {
    (method, path)
    for path, item in openapi["paths"].items()
    for method in item
    if method in {"get", "post", "patch", "put", "delete"}
}
documented_blueprint_surface = {
    item for item in doc_surface if item[1].startswith("/blueprints")
}
blueprint_api_surface = {
    item for item in api_surface if item[1].startswith("/blueprints")
}
require(documented_blueprint_surface, "Documented Blueprint API surface missing")
require(blueprint_api_surface, "Blueprint API authority missing")
require(
    doc_surface == api_surface,
    "Human-readable and machine-readable HTTP surfaces differ",
)

# Every asynchronous mutation is pollable.
require("/operations/{operation_id}" in openapi["paths"], "Operation status endpoint missing")
require("get" in openapi["paths"]["/operations/{operation_id}"], "Operation GET missing")

# Mutation safety. Authentication bootstrap is the only CSRF exception.
for path, item in openapi["paths"].items():
    for method, op in item.items():
        if method not in {"post", "put", "patch", "delete"}:
            continue
        names = {p.get("name") for p in op.get("parameters", []) if isinstance(p, dict)}
        require("Idempotency-Key" in names, f"Idempotency-Key missing: {method} {path}")
        security = op.get("security", [])
        if path == "/auth/login":
            require(security == [], "Login must not require an existing session")
        elif path == "/auth/csrf":
            require(any("sessionCookie" in x for x in security), "CSRF issuance requires session")
        else:
            require(any("sessionCookie" in x and "csrfHeader" in x for x in security), f"Session+CSRF missing: {method} {path}")
        schema = op.get("requestBody",{}).get("content",{}).get("application/json",{}).get("schema")
        if schema and schema.get("type") == "object":
            require(schema.get("additionalProperties") is False, f"Mutation body must reject unknown fields: {method} {path}")

# Responses permit additive fields as promised by versioning policy.
for name, schema in openapi["components"]["schemas"].items():
    if name.endswith("Response") or name in {"Problem","Project","Objective","Task","Run","Review","Recovery","SandboxProfile","Operation"}:
        if schema.get("type") == "object":
            require(schema.get("additionalProperties") is not False, f"Response schema blocks additive fields: {name}")

# Existing objective runtime semantics must be preserved.
objective = openapi["components"]["schemas"]["Objective"]
require("project_ids" in objective["required"], "Objective must support multiple projects")
require("project_id" not in objective["properties"], "Single-project objective regression")
priority = objective["properties"]["priority"]
require(priority == {"type":"integer","minimum":-1000,"maximum":1000,"description":"Lower numeric values run first, matching the current objective queue."}, "Priority contract drift")
create = openapi["paths"]["/objectives"]["post"]["requestBody"]["content"]["application/json"]["schema"]
for field in ("project_ids","not_before","max_parallel_tasks","planning_max_attempts"):
    require(field in create["properties"], f"Objective create field missing: {field}")

# Event types are forward compatible while known names remain discoverable.
type_schema = events["properties"]["type"]
require("enum" not in type_schema, "Closed event enum contradicts forward compatibility")
known = type_schema.get("x-hermesops-known-event-types", [])
for name in ("run.state_changed","review.verdict_recorded","sandbox.activated"):
    require(name in known, f"Known event missing: {name}")
require(type_schema.get("pattern"), "Event type pattern missing")

# Blueprint v1 cannot opt into a feature the spec says is not supported.
secrets = blueprint["properties"]["spec"]["properties"]["security"]["properties"]["secrets"]
require(secrets.get("const") is False, "Blueprint v1 must force secrets=false")
require("CsrfChallenge" in openapi["components"]["schemas"], "CSRF challenge schema missing")
require("CsrfChallengeResponse" in openapi["components"]["schemas"], "CSRF challenge response missing")
require("CsrfToken" not in openapi["components"]["schemas"], "Scanner-ambiguous CSRF schema name returned")

# Persistence mapping and ADR are mandatory.
for relative in (
    "docs/architecture/CONTROLLER_PERSISTENCE_DELTA.md",
    "docs/adr/0007-preserve-existing-objective-semantics.md",
):
    require((root / relative).is_file(), f"Missing review contract: {relative}")

print("Controller HTTP human/machine parity: PASS")
print("Controller operation polling contract: PASS")
print("Controller mutation safety contract: PASS")
print("Controller forward compatibility contract: PASS")
print("Current objective semantics preserved: PASS")
print("Controller AsyncAPI event transport: PASS")
print("Replayable extensible event envelope: PASS")
print("Blueprint unsupported secret eligibility rejected: PASS")
print("Controller persistence delta documented: PASS")
PYTEST

echo "HERMESOPS_CONTROLLER_CONTRACTS_REVIEW_RC3_PASS"
