#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${REPO}/install.sh"
UNINSTALLER="${REPO}/uninstall.sh"

bash -n "$INSTALLER" "$UNINSTALLER"
grep -Fq 'COMPOSE_URL="https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/orchestra.yaml"' "$INSTALLER"
grep -Fq 'MANIFEST_URL="https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/orchestra-release-manifest.json"' "$INSTALLER"
grep -Fq '.publication_state == "accepted"' "$INSTALLER"
grep -Fq '.version == "v0.1.0"' "$INSTALLER"
grep -Fq 'ORCHESTRA_WORKER_IMAGE=%s' "$INSTALLER"
grep -Fq 'ORCHESTRA_DATA_SOURCE=%s' "$INSTALLER"
grep -Fq '"${compose[@]}" up -d' "$INSTALLER"
grep -Fq 'did not become healthy' "$INSTALLER"
grep -Fq 'down --remove-orphans' "$UNINSTALLER"
grep -Fq '[[ "$CONFIRM" == "REMOVE_DATA" ]]' "$UNINSTALLER"

for forbidden in 'git clone' 'git checkout' 'rsync' 'pip install' 'docker compose build'; do
    ! grep -Fq "$forbidden" "$INSTALLER"
done

echo "Orchestra installer uses release assets without Git/source/build: PASS"
echo "Orchestra no-auth installation contract: PASS"
