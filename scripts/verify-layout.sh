#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"

required_directories=(
    "$REPO"
    "${ROOT}/state/hermes-home"
    "${ROOT}/state/controller"
    "${ROOT}/secrets"
    "${ROOT}/workspaces"
    "${ROOT}/project-data"
    "${ROOT}/backups"
    "${ROOT}/logs"
    "${ROOT}/runtime"
)

for directory in "${required_directories[@]}"; do
    [[ -d "$directory" ]] || {
        echo "ABSENT: $directory" >&2
        exit 1
    }
done

for file in \
    "${REPO}/compose/agent.yaml" \
    "${REPO}/config/controller.toml"
do
    [[ -f "$file" ]] || {
        echo "ABSENT: $file" >&2
        exit 1
    }
done

secret_mode="$(stat -c '%a' "${ROOT}/secrets")"

[[ "$secret_mode" == "700" ]] || {
    echo "Permissions incorrectes sur secrets: ${secret_mode}" >&2
    exit 1
}

if [[ -d "${REPO}/.git" ]]; then
    git -C "$REPO" rev-parse --verify HEAD >/dev/null
    echo "Orchestra layout: PASS (git checkout)"
else
    echo "Orchestra layout: PASS (source archive)"
fi
