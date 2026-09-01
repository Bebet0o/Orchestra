#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export PYTHONWARNINGS="error::ResourceWarning"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

python3 -m compileall -q \
    controller_api/review_recovery_reads.py \
    controller_api/review_recovery_probe.py \
    scripts/orchestra-controller-review-recovery-probe.py \
    tests/test_controller_review_recovery_reads.py

python3 tests/test_controller_review_recovery_reads.py
python3 scripts/orchestra-controller-review-recovery-probe.py --help >/dev/null
(
    cd /tmp
    env -u PYTHONPATH \
        python3 "$REPO/scripts/orchestra-controller-review-recovery-probe.py" \
        --help >/dev/null
)

python3 - "$REPO/specs/controller-api-v1.openapi.json" <<'PY'
import json
import sys
from pathlib import Path

contract = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for path in (
    "/reviews",
    "/reviews/{review_id}",
    "/reviews/{review_id}/evidence",
    "/recoveries",
    "/recoveries/{recovery_id}",
):
    if "get" not in contract["paths"].get(path, {}):
        raise SystemExit(f"Missing OpenAPI GET contract: {path}")
for schema in (
    "ReviewListResponse",
    "ReviewResponse",
    "ArtifactListResponse",
    "RecoveryListResponse",
    "RecoveryResponse",
):
    if schema not in contract["components"]["schemas"]:
        raise SystemExit(f"Missing OpenAPI schema: {schema}")
print("Controller review/evidence/recovery OpenAPI subset: PASS")
PY

python3 - \
    "$REPO/controller_api/review_recovery_reads.py" \
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
    "REVIEW_ID_PATTERN",
    "RECOVERY_ID_PATTERN",
    "REVIEW_EXECUTION_ID_PATTERN",
    "INTEGRATION_ID_PATTERN",
    "PUBLIC_TRANSACTION_PATTERN",
    "PUBLIC_EVIDENCE_PATTERN",
    "MAX_INTERNAL_KEY_BYTES",
    "MAX_JSON_BYTES",
    "MAX_JSON_ITEMS",
    "RecursionError",
    "_UNSAFE_FIELD_KEY_MARKERS",
    "hermesops-transaction-reference-v1",
    "list_reviews",
    "get_review_evidence",
    "list_recoveries",
    "resource_revision",
):
    if marker not in store:
        raise SystemExit(f"Review/recovery hardening marker missing: {marker}")
for forbidden in (
    "INSERT INTO review_",
    "UPDATE review_",
    "DELETE FROM review_",
    "INSERT INTO recovery_",
    "UPDATE recovery_",
    "DELETE FROM recovery_",
    "prompt_path",
    "output_path",
    "outer_container_name",
    "sandbox_container_id",
    "Path.open(",
    "read_text(",
    "read_bytes(",
):
    if forbidden in store:
        raise SystemExit(f"Unsafe review/recovery marker: {forbidden}")
for marker in (
    '"review_reads": True',
    '"review_evidence_reads": True',
    '"integration_summary_reads": True',
    '"recovery_reads": True',
    '"raw_review_artifact_reads": False',
):
    if marker not in core:
        raise SystemExit(f"Review/recovery capability marker missing: {marker}")
for route in (
    'path in {"/api/v1/reviews", "/api/v1/recoveries"}',
    'review_evidence_suffix = "/evidence"',
    "service.review_recovery.list_reviews",
    "service.review_recovery.get_review",
    "service.review_recovery.get_review_evidence",
    "service.review_recovery.list_recoveries",
    "service.review_recovery.get_recovery",
):
    if route not in server:
        raise SystemExit(f"Review/recovery route marker missing: {route}")
print("Controller review/integration/recovery read-only contract: PASS")
PY

python3 - "$REPO/tests/test_controller_service.py" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    "CREATE TABLE review_results",
    "CREATE TABLE reviewer_executions",
    "CREATE TABLE integration_executions",
    "CREATE TABLE recovery_executions",
):
    if marker not in text:
        raise SystemExit(f"Review/recovery readiness fixture missing: {marker}")
print("Controller review/recovery readiness fixture: PASS")
PY

python3 - "$REPO/scripts/orchestra-controller-review-recovery-probe.py" <<'PY'
from pathlib import Path
import sys

script = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    'REPO = Path(__file__).resolve().parents[1]',
    'sys.path.insert(0, str(REPO))',
):
    if marker not in script:
        raise SystemExit(f"Review/recovery probe bootstrap missing: {marker}")
print("Controller review/recovery probe standalone bootstrap: PASS")
PY

python3 - "$REPO/controller_api/core.py" "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
server = Path(sys.argv[2]).read_text(encoding="utf-8")
if "def authenticate(self, cookie_header: str | None) -> str:" not in core:
    raise SystemExit("Authentication must return the validated cursor secret.")
if server.count("cursor_secret=session_token") < 5:
    raise SystemExit("Review/recovery cursors are not bound to authentication.")
print("Controller review/recovery cursor authentication binding: PASS")
PY

echo "ORCHESTRA_CONTROLLER_REVIEW_RECOVERY_READS_PASS"
