#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

TARGET_VERSION="v0.2.0"
INSTALL_ROOT="/opt/orchestra"
DATA_ROOT="${INSTALL_ROOT}/data"
TARGET_INSTALL_URL="https://github.com/Bebet0o/Orchestra/releases/download/v0.2.0/install.sh"
TARGET_INSTALL_SHA256="2eb49ba211aaa22b43932a4c0cb47eaa7308cae4bf0025b95c413e139a01af75"
INSTALLER_SOURCE=""
MANIFEST_SOURCE=""
COMPOSE_SOURCE=""
NON_INTERACTIVE=0

usage() {
    cat <<'HELP'
Usage: update.sh [options]

  --installer-file PATH  Local v0.2.0 install.sh (testing; exact SHA still required).
  --manifest-file PATH   Local accepted v0.2.0 manifest passed to install.sh.
  --compose-file PATH    Local canonical Compose file passed to install.sh.
  --non-interactive      Refuse interactive sudo prompts.
  -h, --help             Show this help.

Updates a public Orchestra comfort installation from accepted v0.1.0 or v0.2.0
to v0.2.0 while preserving /opt/orchestra/data. A timestamped pre-update backup
is stored under /opt/orchestra/data/backups/.
HELP
}
while (($#)); do
    case "$1" in
        --installer-file) INSTALLER_SOURCE="${2:?installer path missing}"; shift 2 ;;
        --manifest-file) MANIFEST_SOURCE="${2:?manifest path missing}"; shift 2 ;;
        --compose-file) COMPOSE_SOURCE="${2:?Compose path missing}"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done
sudo_run() {
    if [[ "$EUID" == 0 ]]; then "$@";
    elif [[ "$NON_INTERACTIVE" == 1 ]]; then sudo -n "$@";
    else sudo "$@"; fi
}
for command in curl jq sha256sum docker awk date mktemp; do
    command -v "$command" >/dev/null 2>&1 || { echo "Required update command is missing: $command" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "Docker Compose plugin is required." >&2; exit 1; }
for path in "$INSTALL_ROOT/orchestra.yaml" "$INSTALL_ROOT/orchestra.env" "$INSTALL_ROOT/orchestra-release-manifest.json"; do
    sudo_run test -f "$path" || { echo "Orchestra comfort installation is incomplete: $path" >&2; exit 1; }
    sudo_run test ! -L "$path" || { echo "Refusing symlinked installation authority: $path" >&2; exit 1; }
done
current_manifest="$(mktemp)"
target_installer="$(mktemp)"
cleanup() { rm -f -- "$current_manifest" "$target_installer"; }
trap cleanup EXIT HUP INT TERM
sudo_run cat "$INSTALL_ROOT/orchestra-release-manifest.json" >"$current_manifest"
current_version="$(jq -r '.version // empty' "$current_manifest")"
current_state="$(jq -r '.publication_state // empty' "$current_manifest")"
[[ "$current_state" == "accepted" ]] || { echo "Current installation is not based on an accepted release manifest." >&2; exit 1; }
case "$current_version" in
    v0.1.0|v0.2.0) ;;
    *) echo "Unsupported Orchestra update source: $current_version" >&2; exit 1 ;;
esac
read_env() {
    local key=$1
    sudo_run awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; found=1; exit } END { if (!found) exit 1 }' "$INSTALL_ROOT/orchestra.env"
}
ORCHESTRA_PORT="$(read_env ORCHESTRA_PORT)" || { echo "Current ORCHESTRA_PORT is missing." >&2; exit 1; }
ORCHESTRA_PUBLIC_ORIGIN="$(read_env ORCHESTRA_PUBLIC_ORIGIN)" || { echo "Current ORCHESTRA_PUBLIC_ORIGIN is missing." >&2; exit 1; }
ORCHESTRA_DATA_SOURCE="$(read_env ORCHESTRA_DATA_SOURCE)" || { echo "Current ORCHESTRA_DATA_SOURCE is missing." >&2; exit 1; }
[[ "$ORCHESTRA_PORT" =~ ^[0-9]+$ ]] && ((ORCHESTRA_PORT >= 1 && ORCHESTRA_PORT <= 65535)) || { echo "Current Orchestra port is invalid." >&2; exit 1; }
[[ "$ORCHESTRA_PUBLIC_ORIGIN" =~ ^https?://[^[:space:]]+$ ]] || { echo "Current Orchestra public origin is invalid." >&2; exit 1; }
[[ "$ORCHESTRA_DATA_SOURCE" == "$DATA_ROOT" ]] || { echo "update.sh supports only the public comfort-install data root: $DATA_ROOT" >&2; exit 1; }
if [[ -n "$INSTALLER_SOURCE" ]]; then
    [[ -f "$INSTALLER_SOURCE" ]] || { echo "Installer file not found: $INSTALLER_SOURCE" >&2; exit 1; }
    cp "$INSTALLER_SOURCE" "$target_installer"
else
    curl --fail --silent --show-error --location "$TARGET_INSTALL_URL" -o "$target_installer"
fi
printf '%s  %s\n' "$TARGET_INSTALL_SHA256" "$target_installer" | sha256sum --check --status || { echo "Target v0.2.0 installer SHA-256 mismatch." >&2; exit 1; }
chmod 0755 "$target_installer"
compose=(sudo_run docker compose --project-directory "$INSTALL_ROOT" --env-file "$INSTALL_ROOT/orchestra.env" -f "$INSTALL_ROOT/orchestra.yaml")
"${compose[@]}" config --quiet
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$DATA_ROOT/backups/pre-update-${TARGET_VERSION}-${timestamp}"
sudo_run install -d -m 0750 -o 1000 -g 1000 "$backup_dir"
sudo_run cp -a "$INSTALL_ROOT/orchestra.yaml" "$backup_dir/orchestra.yaml"
sudo_run cp -a "$INSTALL_ROOT/orchestra.env" "$backup_dir/orchestra.env"
sudo_run cp -a "$INSTALL_ROOT/orchestra-release-manifest.json" "$backup_dir/orchestra-release-manifest.json"
"${compose[@]}" stop
if sudo_run test -f "$DATA_ROOT/state/controller/orchestra.db"; then sudo_run cp -a "$DATA_ROOT/state/controller/orchestra.db" "$backup_dir/orchestra.db"; fi
for suffix in -wal -shm; do
    if sudo_run test -f "$DATA_ROOT/state/controller/orchestra.db${suffix}"; then sudo_run cp -a "$DATA_ROOT/state/controller/orchestra.db${suffix}" "$backup_dir/orchestra.db${suffix}"; fi
done
installer_args=(--port "$ORCHESTRA_PORT" --public-origin "$ORCHESTRA_PUBLIC_ORIGIN")
if [[ -n "$MANIFEST_SOURCE" ]]; then installer_args+=(--manifest-file "$MANIFEST_SOURCE"); fi
if [[ -n "$COMPOSE_SOURCE" ]]; then installer_args+=(--compose-file "$COMPOSE_SOURCE"); fi
if [[ "$NON_INTERACTIVE" == 1 ]]; then installer_args+=(--non-interactive); fi
if ! "$target_installer" "${installer_args[@]}"; then
    echo "Orchestra update failed. Pre-update backup: $backup_dir" >&2
    echo "Automatic downgrade is intentionally disabled after migration may have started." >&2
    exit 1
fi
installed_version="$(sudo_run jq -r '.version // empty' "$INSTALL_ROOT/orchestra-release-manifest.json")"
[[ "$installed_version" == "$TARGET_VERSION" ]] || { echo "Installed release manifest did not advance to $TARGET_VERSION." >&2; exit 1; }
sudo_run test -d "$DATA_ROOT" || { echo "Persistent data root disappeared during update." >&2; exit 1; }
echo "ORCHESTRA_UPDATE_PASS"
echo "Updated: ${current_version} -> ${TARGET_VERSION}"
echo "Backup: ${backup_dir}"
