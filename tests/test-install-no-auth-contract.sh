#!/usr/bin/env bash
    set -Eeuo pipefail
    export LC_ALL=C

    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    INSTALLER="${REPO}/install.sh"
    VERIFY_LAYOUT="${REPO}/scripts/verify-layout.sh"

    grep -Fq \
        'auth.json absent; validation des profils IA reportée.' \
        "$INSTALLER"

    grep -Fq \
        'if [[ -f "${ROOT}/state/hermes-home/auth.json" ]]; then' \
        "$INSTALLER"

    grep -Fq \
        '"${REPO}/scripts/orchestra-roles.py" verify-profiles' \
        "$INSTALLER"

    python3 - "$INSTALLER" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")

sync = text.index(
    '"${REPO}/scripts/orchestra-roles.py" sync'
)
condition = text.index(
    'if [[ -f "${ROOT}/state/hermes-home/auth.json" ]]; then',
    sync,
)
verify = text.index(
    '"${REPO}/scripts/orchestra-roles.py" verify-profiles',
    condition,
)
deferred = text.index(
    'auth.json absent; validation des profils IA reportée.',
    verify,
)
registry = text.index(
    '"${REPO}/scripts/orchestra-registry.py" validate',
    deferred,
)

if not sync < condition < verify < deferred < registry:
    raise SystemExit("Invalid no-auth role validation order")

print("Orchestra no-auth installer order: PASS")
PY

    TMP="$(mktemp -d)"
    cleanup() {
        rm -rf "$TMP"
    }
    trap cleanup EXIT

    ROOT="${TMP}/root"
    mkdir -p \
        "${ROOT}/repo/compose" \
        "${ROOT}/repo/config" \
        "${ROOT}/state/hermes-home" \
        "${ROOT}/state/controller" \
        "${ROOT}/secrets" \
        "${ROOT}/workspaces" \
        "${ROOT}/project-data" \
        "${ROOT}/backups" \
        "${ROOT}/logs" \
        "${ROOT}/runtime"

    chmod 0700 "${ROOT}/secrets"

    printf '%s\n' 'services: {}' >"${ROOT}/repo/compose/agent.yaml"
    printf '%s\n' 'schema_version = 1' >"${ROOT}/repo/config/controller.toml"

    [[ ! -e "${ROOT}/repo/.git" ]]

    OUTPUT="$(
        ORCHESTRA_ROOT="$ROOT" \
            "$VERIFY_LAYOUT"
    )"

    grep -Fq \
        'Orchestra layout: PASS (source archive)' \
        <<<"$OUTPUT"

    echo "Orchestra source-archive runtime layout: PASS"
    echo "Orchestra no-auth installation contract: PASS"
