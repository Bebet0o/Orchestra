#!/usr/bin/env bash
set -Eeuo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO" \
  python3 -m unittest -v tests.test_controller_websocket_transport
printf '%s\n' 'ORCHESTRA_CONTROLLER_WEBSOCKET_TRANSPORT_PASS'
