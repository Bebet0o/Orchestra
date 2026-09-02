#!/usr/bin/env bash
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec docker compose \
    --project-directory "${REPO}" \
    -f "${REPO}/compose/orchestra.yaml" \
    "$@"
