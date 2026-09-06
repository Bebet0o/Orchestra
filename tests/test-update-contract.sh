#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATER="$REPO/update.sh"
INSTALLER="$REPO/install.sh"
bash -n "$UPDATER"
expected_installer_sha="$(sha256sum "$INSTALLER" | awk '{print $1}')"
grep -Fq "TARGET_INSTALL_SHA256=\"$expected_installer_sha\"" "$UPDATER"
grep -Fq 'TARGET_VERSION="v0.2.0"' "$UPDATER"
grep -Fq 'v0.1.0|v0.2.0' "$UPDATER"
grep -Fq 'publication_state' "$UPDATER"
grep -Fq '"${compose[@]}" stop' "$UPDATER"
grep -Fq 'pre-update-${TARGET_VERSION}-${timestamp}' "$UPDATER"
grep -Fq 'orchestra.db' "$UPDATER"
grep -Fq -- '--public-origin "$ORCHESTRA_PUBLIC_ORIGIN"' "$UPDATER"
grep -Fq 'ORCHESTRA_DATA_SOURCE" == "$DATA_ROOT' "$UPDATER"
grep -Fq 'ORCHESTRA_UPDATE_PASS' "$UPDATER"
for forbidden in 'rm -rf /opt/orchestra/data' 'down -v' '--remove-data' 'git clone' 'docker compose build'; do ! grep -Fq -- "$forbidden" "$UPDATER"; done
echo "Orchestra update contract: PASS"
