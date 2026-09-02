#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

INSTALL_ROOT="/opt/orchestra"
DATA_ROOT="${INSTALL_ROOT}/data"
COMPOSE_URL="https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/orchestra.yaml"
COMPOSE_SOURCE=""
MANIFEST_URL="https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/orchestra-release-manifest.json"
MANIFEST_SOURCE=""
ORCHESTRA_IMAGE=""
ORCHESTRA_RUNTIME_IMAGE=""
APPLICATION_OVERRIDE=0
RUNTIME_OVERRIDE=0
ORCHESTRA_PORT="8080"
SKIP_START=0
NON_INTERACTIVE=0

usage() {
    cat <<'HELP'
Usage: install.sh [options]

  --orchestra-image REF  Immutable application image override.
  --runtime-image REF    Immutable private runtime image override.
  --port PORT            Host Console port (default: 8080).
  --manifest-url URL     Accepted release manifest URL.
  --manifest-file PATH   Local accepted manifest (development/testing only).
  --compose-url URL      Canonical Compose release asset URL.
  --compose-file PATH    Local canonical Compose file (development/testing only).
  --skip-start           Prepare the deployment without starting it.
  --non-interactive      Refuse interactive sudo prompts.
  -h, --help             Show this help.

Persistent host data is stored in /opt/orchestra/data and mounted at
/var/lib/orchestra inside both official services.
HELP
}

while (($#)); do
    case "$1" in
        --orchestra-image) ORCHESTRA_IMAGE="${2:?image reference missing}"; APPLICATION_OVERRIDE=1; shift 2 ;;
        --runtime-image) ORCHESTRA_RUNTIME_IMAGE="${2:?runtime image reference missing}"; RUNTIME_OVERRIDE=1; shift 2 ;;
        --port) ORCHESTRA_PORT="${2:?port missing}"; shift 2 ;;
        --manifest-url) MANIFEST_URL="${2:?manifest URL missing}"; shift 2 ;;
        --manifest-file) MANIFEST_SOURCE="${2:?manifest path missing}"; shift 2 ;;
        --compose-url) COMPOSE_URL="${2:?Compose URL missing}"; shift 2 ;;
        --compose-file) COMPOSE_SOURCE="${2:?Compose path missing}"; shift 2 ;;
        --skip-start) SKIP_START=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$ORCHESTRA_PORT" =~ ^[0-9]+$ ]] && ((ORCHESTRA_PORT >= 1 && ORCHESTRA_PORT <= 65535)) || {
    echo "Invalid host port: $ORCHESTRA_PORT" >&2
    exit 1
}
sudo_run() {
    if [[ "$EUID" == 0 ]]; then
        "$@"
    elif [[ "$NON_INTERACTIVE" == 1 ]]; then
        sudo -n "$@"
    else
        sudo "$@"
    fi
}

. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    debian:1[2-9]*|ubuntu:22.04|ubuntu:2[4-9].*) ;;
    *) echo "Supported hosts are Debian 12+ and Ubuntu 22.04+ on amd64." >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) echo "Orchestra v0.1.0 requires amd64." >&2; exit 1 ;;
esac

install_docker() {
    sudo_run install -m 0755 -d /etc/apt/keyrings
    temporary_key="$(mktemp)"
    curl --fail --silent --show-error --location \
        "https://download.docker.com/linux/${ID}/gpg" -o "$temporary_key"
    sudo_run install -m 0644 "$temporary_key" /etc/apt/keyrings/docker.asc
    rm -f "$temporary_key"
    architecture="$(dpkg --print-architecture)"
    codename="${VERSION_CODENAME:?VERSION_CODENAME missing}"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$architecture" "$ID" "$codename" |
        sudo_run tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo_run apt-get update
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    sudo_run apt-get update
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl jq
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    install_docker
fi
sudo_run systemctl enable --now docker.service containerd.service
sudo_run docker version >/dev/null
sudo_run docker compose version >/dev/null

sudo_run install -d -m 0750 "$INSTALL_ROOT"
sudo_run install -d -m 0750 -o 1000 -g 1000 \
    "$DATA_ROOT" "$DATA_ROOT/state" "$DATA_ROOT/runtime" \
    "$DATA_ROOT/workspaces" "$DATA_ROOT/project-data" "$DATA_ROOT/backups" "$DATA_ROOT/logs"

manifest_tmp="$(mktemp)"
if [[ -n "$MANIFEST_SOURCE" ]]; then
    [[ -f "$MANIFEST_SOURCE" ]] || { echo "Manifest file not found: $MANIFEST_SOURCE" >&2; exit 1; }
    cp "$MANIFEST_SOURCE" "$manifest_tmp"
else
    curl --fail --silent --show-error --location "$MANIFEST_URL" -o "$manifest_tmp"
fi
jq -e '
    .schema_version == 1 and
    .publication_state == "accepted" and
    .version == "v0.1.0" and
    .platform == "linux/amd64" and
    (.source_revision | type == "string" and test("^[0-9a-f]{40}$")) and
    .application.repository == "ghcr.io/bebet0o/orchestra" and
    (.application.digest | type == "string" and test("^sha256:[0-9a-f]{64}$")) and
    .application.image_reference == (.application.repository + "@" + .application.digest) and
    .runtime.repository == "ghcr.io/bebet0o/orchestra-runtime" and
    (.runtime.digest | type == "string" and test("^sha256:[0-9a-f]{64}$")) and
    .runtime.image_reference == (.runtime.repository + "@" + .runtime.digest) and
    .worker.repository == "ghcr.io/bebet0o/orchestra-worker" and
    .worker.digest == "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49" and
    .worker.image_reference == "ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
' "$manifest_tmp" >/dev/null || {
    echo "Release manifest is not accepted immutable Orchestra authority." >&2
    exit 1
}
manifest_application="$(jq -r '.application.image_reference' "$manifest_tmp")"
manifest_runtime="$(jq -r '.runtime.image_reference' "$manifest_tmp")"
ORCHESTRA_WORKER_IMAGE="$(jq -r '.worker.image_reference' "$manifest_tmp")"
sudo_run install -m 0644 "$manifest_tmp" "$INSTALL_ROOT/orchestra-release-manifest.json"
rm -f "$manifest_tmp"
ORCHESTRA_IMAGE="${ORCHESTRA_IMAGE:-$manifest_application}"
ORCHESTRA_RUNTIME_IMAGE="${ORCHESTRA_RUNTIME_IMAGE:-$manifest_runtime}"
[[ "$APPLICATION_OVERRIDE" == "$RUNTIME_OVERRIDE" ]] || {
    echo "Application and runtime development overrides must be supplied together." >&2
    exit 1
}
for reference in "$ORCHESTRA_WORKER_IMAGE"; do
    [[ "$reference" =~ ^[a-z0-9.-]+(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$ ]] || {
        echo "Immutable image authority required: $reference" >&2
        exit 1
    }
done
if [[ "$APPLICATION_OVERRIDE" == 0 ]]; then
    [[ "$ORCHESTRA_IMAGE" == "$manifest_application" && "$ORCHESTRA_RUNTIME_IMAGE" == "$manifest_runtime" ]]
else
    for reference in "$ORCHESTRA_IMAGE" "$ORCHESTRA_RUNTIME_IMAGE"; do
        [[ "$reference" != *":latest" && "$reference" != "latest" && "$reference" != *[[:space:]]* ]] || {
            echo "Unsafe development image override: $reference" >&2
            exit 1
        }
    done
fi

compose_tmp="$(mktemp)"
if [[ -n "$COMPOSE_SOURCE" ]]; then
    [[ -f "$COMPOSE_SOURCE" ]] || { echo "Compose file not found: $COMPOSE_SOURCE" >&2; exit 1; }
    cp "$COMPOSE_SOURCE" "$compose_tmp"
else
    curl --fail --silent --show-error --location "$COMPOSE_URL" -o "$compose_tmp"
fi
grep -Eq '^  orchestra:$' "$compose_tmp"
grep -Eq '^  orchestra-runtime:$' "$compose_tmp"
sudo_run install -m 0644 "$compose_tmp" "$INSTALL_ROOT/orchestra.yaml"
rm -f "$compose_tmp"

environment_tmp="$(mktemp)"
{
    printf 'ORCHESTRA_IMAGE=%s\n' "$ORCHESTRA_IMAGE"
    printf 'ORCHESTRA_RUNTIME_IMAGE=%s\n' "$ORCHESTRA_RUNTIME_IMAGE"
    printf 'ORCHESTRA_WORKER_IMAGE=%s\n' "$ORCHESTRA_WORKER_IMAGE"
    printf 'ORCHESTRA_PORT=%s\n' "$ORCHESTRA_PORT"
    printf 'ORCHESTRA_PUBLIC_ORIGIN=http://127.0.0.1:%s\n' "$ORCHESTRA_PORT"
    printf 'ORCHESTRA_DATA_SOURCE=%s\n' "$DATA_ROOT"
} >"$environment_tmp"
sudo_run install -m 0640 "$environment_tmp" "$INSTALL_ROOT/orchestra.env"
rm -f "$environment_tmp"

compose=(sudo_run docker compose --project-directory "$INSTALL_ROOT" --env-file "$INSTALL_ROOT/orchestra.env" -f "$INSTALL_ROOT/orchestra.yaml")
"${compose[@]}" config --quiet

if [[ "$SKIP_START" == 0 ]]; then
    "${compose[@]}" pull
    "${compose[@]}" up -d
    healthy=""
    for _ in $(seq 1 120); do
        container_id="$("${compose[@]}" ps -q orchestra)"
        healthy="$(sudo_run docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
        [[ "$healthy" == healthy ]] && break
        sleep 2
    done
    [[ "$healthy" == healthy ]] || {
        "${compose[@]}" logs --no-color --tail 200 >&2 || true
        echo "Orchestra appliance did not become healthy." >&2
        exit 1
    }
fi

echo "ORCHESTRA_INSTALL_PASS"
echo "Open: http://127.0.0.1:${ORCHESTRA_PORT}"
echo "Data: ${DATA_ROOT}"
