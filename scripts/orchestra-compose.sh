#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
export ORCHESTRA_ROOT="$ROOT"
export ORCHESTRA_UID="${ORCHESTRA_UID:-$(id -u)}"
export ORCHESTRA_GID="${ORCHESTRA_GID:-$(id -g)}"

exec docker compose \
    --project-directory "${REPO}/compose" \
    --env-file "${REPO}/compose/images.lock.env" \
    -f "${REPO}/compose/agent.yaml" \
    "$@"
