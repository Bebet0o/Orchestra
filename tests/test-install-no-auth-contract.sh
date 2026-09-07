#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${REPO}/install.sh"
UNINSTALLER="${REPO}/uninstall.sh"
UPDATER="${REPO}/update.sh"

bash -n "$INSTALLER" "$UPDATER" "$UNINSTALLER"
grep -Fq 'COMPOSE_URL="https://raw.githubusercontent.com/Bebet0o/Orchestra/v0.2.0/compose/orchestra.yaml"' "$INSTALLER"
grep -Fq 'MANIFEST_URL="https://raw.githubusercontent.com/Bebet0o/Orchestra/v0.2.0/config/releases/v0.2.0.manifest.json"' "$INSTALLER"
grep -Fq '.publication_state == "accepted"' "$INSTALLER"
grep -Fq '.version == "v0.2.0"' "$INSTALLER"
grep -Fq 'ORCHESTRA_WORKER_IMAGE=%s' "$INSTALLER"
grep -Fq 'ORCHESTRA_DATA_SOURCE=%s' "$INSTALLER"
grep -Fq '"${compose[@]}" up -d' "$INSTALLER"
grep -Fq -- '--public-origin' "$INSTALLER"
grep -Fq 'ORCHESTRA_PUBLIC_ORIGIN=%s' "$INSTALLER"
grep -Fq 'did not become healthy' "$INSTALLER"
grep -Fq 'down --remove-orphans' "$UNINSTALLER"
grep -Fq '[[ "$CONFIRM" == "REMOVE_DATA" ]]' "$UNINSTALLER"

for forbidden in 'git clone' 'git checkout' 'rsync' 'pip install' 'docker compose build'; do
    ! grep -Fq "$forbidden" "$INSTALLER"
done

manifest="$REPO/config/releases/v0.2.0.manifest.json"
[[ -f "$manifest" ]]
printf '%s  %s\n' '54e1ea7258511c0bcc947645628c709fdd25a00d9221267ede1e945717ce74ad' "$manifest" | sha256sum --check --status
echo "Orchestra installer uses immutable tagged release authority without Git/source/build: PASS"
echo "Orchestra no-auth installation contract: PASS"
