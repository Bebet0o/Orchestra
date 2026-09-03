#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$REPO" python3 "$REPO/tests/test_appliance_distribution.py"
echo "ORCHESTRA_DISTRIBUTION_CONTRACT_PASS"
