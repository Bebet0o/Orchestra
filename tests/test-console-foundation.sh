#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "${REPO}/scripts/orchestra-console-build.py" check \
    --source "${REPO}/console/src" \
    --expected "${REPO}/console/dist"

python3 -m unittest -v tests.test_console_service

grep -Fq "Orchestra Console" "${REPO}/console/dist/index.html"
grep -Fq "connect-src 'self'" "${REPO}/scripts/orchestra-console.py"
grep -Fq 'import { ControllerClientError, createControllerClient }' \
    "${REPO}/console/src/app.js"
grep -Fq 'fetch(' "${REPO}/console/src/controller-client.js"

! grep -RInE '(WebSocket\(|localStorage|sessionStorage|indexedDB|eval\(|new Function)' \
    "${REPO}/console/src" "${REPO}/console/dist/assets"

grep -Fq '  orchestra:' "${REPO}/compose/orchestra.yaml"
grep -Fq 'processes["console"] = spawn' "${REPO}/scripts/orchestra-appliance.py"
grep -Fq '"--public-bind"' "${REPO}/scripts/orchestra-appliance.py"

echo "ORCHESTRA_CONSOLE_WEB_FOUNDATION_PASS"
