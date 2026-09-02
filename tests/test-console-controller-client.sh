#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "${REPO}/scripts/orchestra-console-build.py" check \
    --source "${REPO}/console/src" \
    --expected "${REPO}/console/dist"

PYTHONPATH="$REPO" python3 -m unittest -v tests.test_console_controller_client

CLIENT_SOURCE="${REPO}/console/src/controller-client.js"
APP_SOURCE="${REPO}/console/src/app.js"

[[ "$(grep -RIlF 'fetch(' "${REPO}/console/src")" == "$CLIENT_SOURCE" ]]
grep -Fq 'credentials: "same-origin"' "$CLIENT_SOURCE"
grep -Fq 'cache: "no-store"' "$CLIENT_SOURCE"
grep -Fq 'redirect: "error"' "$CLIENT_SOURCE"
grep -Fq 'crypto.randomUUID' "$CLIENT_SOURCE"
grep -Fq 'createControllerClient' "$APP_SOURCE"

! grep -RInE '(localStorage|sessionStorage|indexedDB|WebSocket\(|eval\(|new Function)' \
    "${REPO}/console/src" "${REPO}/console/dist/assets"
! grep -RInF '127.0.0.1:8765' \
    "${REPO}/console/src" "${REPO}/console/dist/assets"

grep -Fq "connect-src 'self'" "${REPO}/scripts/orchestra-console.py"
grep -Fq "form-action 'self'" "${REPO}/scripts/orchestra-console.py"
grep -Fq 'CONTROLLER_ROUTES' "${REPO}/scripts/orchestra-console.py"

grep -Fq '  orchestra:' "${REPO}/compose/orchestra.yaml"
grep -Fq 'condition: service_healthy' "${REPO}/compose/orchestra.yaml"
grep -Fq 'processes["console"] = spawn' "${REPO}/scripts/orchestra-appliance.py"

echo "ORCHESTRA_CONSOLE_CONTROLLER_CLIENT_PASS"
