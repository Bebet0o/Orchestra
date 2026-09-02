#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export PYTHONWARNINGS="error::ResourceWarning"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

python3 -m compileall -q \
    controller_api \
    scripts/orchestra-controller-api.py \
    scripts/orchestra-controller-objective-probe.py \
    scripts/orchestra-controller-objective-command-probe.py \
    scripts/orchestra-controller-review-command-probe.py \
    tests/test_controller_api.py

python3 tests/test_controller_api.py

python3 scripts/orchestra-controller-api.py --help >/dev/null
python3 scripts/orchestra-controller-api.py serve --help >/dev/null
python3 scripts/orchestra-controller-api.py check --help >/dev/null

python3 - "$REPO/specs/controller-api-v1.openapi.json" <<'PY'
import json
import sys
from pathlib import Path

contract = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
implemented = (
    ("/system/health", "get"),
    ("/system/status", "get"),
    ("/system/capabilities", "get"),
    ("/projects", "get"),
    ("/projects/{project_id}", "get"),
    ("/objectives", "get"),
    ("/objectives", "post"),
    ("/objectives/{objective_id}", "get"),
    ("/objectives/{objective_id}/commands/{command}", "post"),
    ("/reviews/{review_id}/commands/{command}", "post"),
    ("/projects/{project_id}/objectives", "get"),
    ("/operations/{operation_id}", "get"),
    ("/auth/csrf", "post"),
    ("/auth/login", "post"),
    ("/auth/session", "get"),
    ("/auth/logout", "post"),
)
for path, method in implemented:
    if path not in contract["paths"]:
        raise SystemExit(f"Missing OpenAPI path: {path}")
    if method not in contract["paths"][path]:
        raise SystemExit(f"Missing OpenAPI method: {method.upper()} {path}")

print("Controller API implemented OpenAPI subset: PASS")
PY

if grep -R -n -E \
    '(^|[^A-Za-z])(Flask|FastAPI|Django|aiohttp|uvicorn)([^A-Za-z]|$)' \
    controller_api scripts/orchestra-controller-api.py
then
    echo "Unexpected third-party web framework dependency." >&2
    exit 1
fi

echo "Controller API stdlib dependency contract: PASS"
python3 - "$REPO/controller_api/core.py" "$REPO/controller_api/server.py" <<'PY'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
server = Path(sys.argv[2]).read_text(encoding="utf-8")

required_core = (
    "mode=ro",
    "PRAGMA query_only = ON",
    "O_NOFOLLOW",
    "st_nlink != 1",
    "metadata.st_uid != os.geteuid()",
)
for marker in required_core:
    if marker not in core:
        raise SystemExit(f"Missing security marker in core.py: {marker}")

for forbidden in ("repo_path\":", "data_path\":", "PRAGMA quick_check"):
    if forbidden in core:
        raise SystemExit(f"Forbidden API/runtime marker remains: {forbidden}")

required_server = (
    "BoundedSemaphore",
    "request.settimeout",
    "max_num_fields=MAX_QUERY_FIELDS",
    "Connection\", \"close",
    "do_TRACE = _method_not_allowed",
    "Misdirected request",
)
for marker in required_server:
    if marker not in server:
        raise SystemExit(f"Missing HTTP hardening marker: {marker}")

print("Controller API adversarial hardening contract: PASS")
PY

echo "Controller API bounded mutation foundation: PASS"
echo "ORCHESTRA_CONTROLLER_API_SKELETON_PASS"
