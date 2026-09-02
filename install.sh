#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRA_ROOT="/opt/orchestra"
LEGACY_ROOT="/opt/docker/hermesops"
TARGET_USER=""
AUTH_FILE=""
NON_INTERACTIVE=0
UPGRADE=0
SKIP_START=0
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

PLATFORM_SUPPORT="${SOURCE}/scripts/platform-support.sh"
[[ -r "$PLATFORM_SUPPORT" ]] || {
    echo "Contrat de plateforme absent: $PLATFORM_SUPPORT" >&2
    exit 1
}
# shellcheck disable=SC1090
. "$PLATFORM_SUPPORT"

usage() {
    cat <<'HELP'
Usage: ./install.sh [options]

  --user USER          Utilisateur opérateur Orchestra.
  --auth-file PATH     auth.json OpenAI Codex à installer.
  --upgrade            Autoriser une mise à niveau divergente sauvegardée.
  --skip-start         Installer sans démarrer la pile Compose.
  --non-interactive    Refuser toute demande sudo interactive.
  -h, --help           Afficher cette aide.

La racine Orchestra est fixe : /opt/orchestra
HELP
}

while (($#)); do
    case "$1" in
        --user) TARGET_USER="${2:?Utilisateur manquant}"; shift 2 ;;
        --auth-file) AUTH_FILE="${2:?Chemin auth manquant}"; shift 2 ;;
        --upgrade) UPGRADE=1; shift ;;
        --skip-start) SKIP_START=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$TARGET_USER" ]]; then
    if [[ "$EUID" == 0 ]]; then
        TARGET_USER="${SUDO_USER:-}"
    else
        TARGET_USER="$(id -un)"
    fi
fi
[[ -n "$TARGET_USER" ]] || {
    echo "Exécution root: préciser --user USER." >&2
    exit 1
}
id "$TARGET_USER" >/dev/null
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ "$TARGET_UID" =~ ^[0-9]+$ && "$TARGET_GID" =~ ^[0-9]+$ ]] || {
    echo "Identité numérique de l'utilisateur cible invalide." >&2
    exit 1
}

# A legacy installation requires an explicit, separately reviewed migration.
# This gate intentionally runs before package, filesystem, service or group mutation.
LEGACY_UNITS=(
    hermesops-controller-api.service
    hermesops-console.service
    hermesops-notifier.service
    hermesops-orchestrator.service
    hermesops-supervisor.service
)
LEGACY_FOUND=()
[[ ! -e "$LEGACY_ROOT" ]] || LEGACY_FOUND+=("$LEGACY_ROOT")
for unit in "${LEGACY_UNITS[@]}"; do
    for candidate in \
        "${TARGET_HOME}/.config/systemd/user/${unit}" \
        "${TARGET_HOME}/.config/systemd/user/default.target.wants/${unit}"
    do
        [[ ! -e "$candidate" ]] || LEGACY_FOUND+=("$candidate")
    done
done
if ((${#LEGACY_FOUND[@]})); then
    echo "Installation HermesOps historique détectée; aucune mutation effectuée." >&2
    printf '  %s\n' "${LEGACY_FOUND[@]}" >&2
    echo "Une procédure de migration explicite est requise avant Orchestra." >&2
    exit 1
fi

[[ "$ORCHESTRA_ROOT" == "/opt/orchestra" && ! -L "$ORCHESTRA_ROOT" ]] || {
    echo "La racine Orchestra doit être /opt/orchestra et ne peut pas être un lien." >&2
    exit 1
}
if [[ "$EUID" != 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    echo "sudo est requis pour une installation lancée sans root." >&2
    exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
orchestra_os_supported "${ID:-}" "${VERSION_ID:-}" || {
    echo "Système non pris en charge: ${ID:-inconnu} ${VERSION_ID:-inconnue}; $(orchestra_platform_contract) requis." >&2
    exit 1
}
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
orchestra_arch_supported "$ARCH" || {
    echo "Architecture amd64 requise." >&2
    exit 1
}
DOCKER_REPOSITORY_FAMILY="$(orchestra_docker_repository_family "$ID")"
[[ "${VERSION_CODENAME:-}" =~ ^[a-z][a-z0-9-]*$ ]] || {
    echo "VERSION_CODENAME invalide ou absent dans /etc/os-release." >&2
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

user_run() {
    if [[ "$(id -u)" == "$TARGET_UID" ]]; then
        "$@"
    elif [[ "$EUID" == 0 ]]; then
        runuser -u "$TARGET_USER" -- env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" "$@"
    elif [[ "$NON_INTERACTIVE" == 1 ]]; then
        sudo -n -u "$TARGET_USER" env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" "$@"
    else
        sudo -u "$TARGET_USER" env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" "$@"
    fi
}

REPORT_DIR="${ORCHESTRA_ROOT}/runtime/install-reports"
REPORT="${REPORT_DIR}/install-${STAMP}.log"
STATUS="${REPORT_DIR}/install-${STAMP}.status"
sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$REPORT_DIR"
sudo_run touch "$REPORT" "$STATUS"
sudo_run chown "$TARGET_USER:$TARGET_GROUP" "$REPORT" "$STATUS"
sudo_run chmod 0640 "$REPORT" "$STATUS"
exec > >(tee -a "$REPORT") 2>&1
printf 'RUNNING\n' >"$STATUS"

on_error() {
    rc=$?
    trap - ERR
    printf 'FAILED rc=%s\n' "$rc" >"$STATUS"
    echo "Installation échouée. Rapport: $REPORT" >&2
    exit "$rc"
}
trap on_error ERR

echo "Orchestra installation ${STAMP}"
echo "Source      : $SOURCE"
echo "Destination : $ORCHESTRA_ROOT"
echo "Utilisateur : $TARGET_USER ($TARGET_UID:$TARGET_GID)"

BASE_PACKAGES=(ca-certificates curl git gzip python3 python3-yaml rsync sqlite3 util-linux)
MISSING_PACKAGES=()
for package in "${BASE_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null |
        grep -Fq 'install ok installed' || MISSING_PACKAGES+=("$package")
done
if ((${#MISSING_PACKAGES[@]})); then
    sudo_run apt-get update
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends "${MISSING_PACKAGES[@]}"
fi
command -v runuser >/dev/null 2>&1 || {
    echo "runuser reste absent après installation de util-linux." >&2
    exit 1
}

HOST_LOCK="${SOURCE}/config/host-packages.lock.toml"
readarray -t HOST_VERSIONS < <(
    python3 - "$HOST_LOCK" <<'PY'
import sys
import tomllib
from pathlib import Path
with Path(sys.argv[1]).open("rb") as stream:
    data = tomllib.load(stream)
print(data["docker_ce"])
print(data["docker_ce_cli"])
print(data["docker_compose_plugin"])
PY
)
DOCKER_CE_VERSION="${HOST_VERSIONS[0]}"
DOCKER_CLI_VERSION="${HOST_VERSIONS[1]}"
DOCKER_COMPOSE_VERSION="${HOST_VERSIONS[2]}"

configure_docker_repository() {
    sudo_run install -m 0755 -d /etc/apt/keyrings
    local key_tmp source_tmp
    key_tmp="$(mktemp)"
    source_tmp="$(mktemp)"
    curl --fail --silent --show-error --location \
        "https://download.docker.com/linux/${DOCKER_REPOSITORY_FAMILY}/gpg" \
        -o "$key_tmp"
    sudo_run install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc
    {
        printf 'Types: deb\n'
        printf 'URIs: https://download.docker.com/linux/%s\n' "$DOCKER_REPOSITORY_FAMILY"
        printf 'Suites: %s\n' "$VERSION_CODENAME"
        printf 'Components: stable\n'
        printf 'Architectures: %s\n' "$(dpkg --print-architecture)"
        printf 'Signed-By: /etc/apt/keyrings/docker.asc\n'
    } >"$source_tmp"
    sudo_run install -m 0644 "$source_tmp" /etc/apt/sources.list.d/docker.sources
    rm -f "$key_tmp" "$source_tmp"
    sudo_run apt-get update
}

resolve_locked_apt_version() {
    local package="${1:?Paquet manquant}"
    local upstream="${2:?Version amont manquante}"
    local selected
    selected="$(
        apt-cache madison "$package" | awk '{print $3}' |
            orchestra_select_locked_apt_version "$upstream"
    )" || {
        echo "Version amont verrouillée indisponible ou ambiguë pour ${package}: ${upstream}" >&2
        return 1
    }
    printf '%s\n' "$selected"
}

install_docker_engine() {
    local conflicts=()
    local package
    for package in docker.io docker-compose docker-doc podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$package" 2>/dev/null |
           grep -Fq 'install ok installed'; then
            conflicts+=("$package")
        fi
    done
    if ((${#conflicts[@]})); then
        echo "Paquets Docker incompatibles détectés: ${conflicts[*]}" >&2
        echo "Orchestra refuse de les supprimer automatiquement." >&2
        exit 1
    fi
    configure_docker_repository
    local engine cli compose
    engine="$(resolve_locked_apt_version docker-ce "$DOCKER_CE_VERSION")"
    cli="$(resolve_locked_apt_version docker-ce-cli "$DOCKER_CLI_VERSION")"
    compose="$(resolve_locked_apt_version docker-compose-plugin "$DOCKER_COMPOSE_VERSION")"
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        "docker-ce=${engine}" "docker-ce-cli=${cli}" containerd.io \
        docker-buildx-plugin "docker-compose-plugin=${compose}"
}

if ! command -v docker >/dev/null 2>&1; then
    install_docker_engine
elif ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null |
     grep -Fq 'install ok installed'; then
    echo "Une installation Docker non officielle est présente." >&2
    echo "Orchestra exige les paquets Docker CE officiels." >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    configure_docker_repository
    COMPOSE_PACKAGE_VERSION="$(resolve_locked_apt_version docker-compose-plugin "$DOCKER_COMPOSE_VERSION")"
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        "docker-compose-plugin=${COMPOSE_PACKAGE_VERSION}"
fi
sudo_run systemctl enable --now docker.service containerd.service
sudo_run docker version >/dev/null

sudo_run groupadd -f docker
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -Fxq docker; then
    sudo_run usermod -aG docker "$TARGET_USER"
    printf 'RELOGIN_REQUIRED\n' >"$STATUS"
    trap - ERR
    echo "ORCHESTRA_INSTALL_RELOGIN_REQUIRED"
    echo "Reconnecter la session de $TARGET_USER puis relancer install.sh."
    exit 20
fi
user_run docker version >/dev/null
"${SOURCE}/validate.sh" --static

for path in \
    "$ORCHESTRA_ROOT" "$ORCHESTRA_ROOT/state" \
    "$ORCHESTRA_ROOT/state/controller" "$ORCHESTRA_ROOT/state/sandboxes" \
    "$ORCHESTRA_ROOT/runtime" "$ORCHESTRA_ROOT/runtime/sandbox-engine-socket" \
    "$ORCHESTRA_ROOT/workspaces" "$ORCHESTRA_ROOT/project-data" \
    "$ORCHESTRA_ROOT/backups" "$ORCHESTRA_ROOT/logs"
do
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$path"
done
sudo_run install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_GROUP" \
    "$ORCHESTRA_ROOT/state/hermes-home" "$ORCHESTRA_ROOT/secrets"

REPO="${ORCHESTRA_ROOT}/repo"
BACKUP_DIR="${ORCHESTRA_ROOT}/backups/installations/${STAMP}"
if [[ -d "$REPO/.git" ]]; then
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$BACKUP_DIR"
    user_run git -C "$REPO" bundle create \
        "${BACKUP_DIR}/orchestra-before-install.bundle" --all
    user_run git -C "$REPO" bundle verify \
        "${BACKUP_DIR}/orchestra-before-install.bundle"
    if [[ -f "${ORCHESTRA_ROOT}/state/controller/orchestra.db" ]]; then
        user_run sqlite3 "${ORCHESTRA_ROOT}/state/controller/orchestra.db" \
            ".backup '${BACKUP_DIR}/controller-before-install.sqlite'"
        user_run sqlite3 "${BACKUP_DIR}/controller-before-install.sqlite" \
            'PRAGMA quick_check;' | grep -Fxq ok
    fi
fi

SOURCE_REAL="$(readlink -f "$SOURCE")"
TARGET_REAL="$(readlink -f "$REPO" 2>/dev/null || true)"
if [[ ! -d "$REPO/.git" ]]; then
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$REPO"
    sudo_run rsync -a "$SOURCE/" "$REPO/"
    sudo_run chown -R "$TARGET_USER:$TARGET_GROUP" "$REPO"
elif [[ "$SOURCE_REAL" != "$TARGET_REAL" ]]; then
    SOURCE_HEAD="$(git -C "$SOURCE" rev-parse HEAD 2>/dev/null || true)"
    TARGET_HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$SOURCE_HEAD" != "$TARGET_HEAD" && "$UPGRADE" != 1 ]]; then
        echo "Installation existante divergente; --upgrade requis après revue." >&2
        exit 1
    fi
    sudo_run rsync -a --exclude='config/projects.d/*.toml' "$SOURCE/" "$REPO/"
    sudo_run chown -R "$TARGET_USER:$TARGET_GROUP" "$REPO"
fi

umask 077
API_KEY=""
[[ ! -f "${ORCHESTRA_ROOT}/secrets/agent.env" ]] || \
    API_KEY="$(sed -n 's/^API_SERVER_KEY=//p' "${ORCHESTRA_ROOT}/secrets/agent.env" | head -n 1)"
if [[ -z "$API_KEY" ]]; then
    API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    printf 'API_SERVER_KEY=%s\n' "$API_KEY" >"${ORCHESTRA_ROOT}/secrets/agent.env"
fi
if [[ ! -f "${ORCHESTRA_ROOT}/secrets/webui.env" ]]; then
    WEBUI_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    {
        printf 'HERMES_WEBUI_GATEWAY_API_KEY=%s\n' "$API_KEY"
        printf 'HERMES_WEBUI_PASSWORD=%s\n' "$WEBUI_PASSWORD"
    } >"${ORCHESTRA_ROOT}/secrets/webui.env"
fi
sudo_run chmod 0600 "${ORCHESTRA_ROOT}/secrets/agent.env" "${ORCHESTRA_ROOT}/secrets/webui.env"
sudo_run chown "$TARGET_USER:$TARGET_GROUP" \
    "${ORCHESTRA_ROOT}/secrets/agent.env" "${ORCHESTRA_ROOT}/secrets/webui.env"

if [[ -n "$AUTH_FILE" ]]; then
    [[ -f "$AUTH_FILE" ]] || { echo "auth.json absent: $AUTH_FILE" >&2; exit 1; }
    sudo_run install -m 0600 -o "$TARGET_USER" -g "$TARGET_GROUP" \
        "$AUTH_FILE" "${ORCHESTRA_ROOT}/state/hermes-home/auth.json"
fi

ORCHESTRA_ENV=(
    ORCHESTRA_ROOT="$ORCHESTRA_ROOT"
    ORCHESTRA_UID="$TARGET_UID"
    ORCHESTRA_GID="$TARGET_GID"
)
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-controller-session.py" ensure
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-db.py" migrate
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-db.py" integrity
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-controller-operator.py" ensure
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-roles.py" sync
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-registry.py" validate
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-registry.py" sync
user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-compose.sh" config --quiet

if [[ "$SKIP_START" == 0 ]]; then
    user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-compose.sh" \
        pull sandbox-engine hermes-agent hermes-webui
    user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-compose.sh" \
        build controller
    user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-compose.sh" \
        up -d sandbox-engine

    health=""
    for _ in $(seq 1 60); do
        health="$(user_run docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' orchestra-sandbox-engine 2>/dev/null || true)"
        [[ "$health" == "healthy" ]] && break
        sleep 2
    done
    [[ "$health" == "healthy" ]] || { echo "sandbox-engine non sain." >&2; exit 1; }

    PRIVATE_DOCKER_HOST="unix://${ORCHESTRA_ROOT}/runtime/sandbox-engine-socket/docker.sock"
    HERMES_AGENT_IMAGE="$(sed -n 's/^HERMES_AGENT_IMAGE=//p' "${REPO}/compose/images.lock.env")"
    user_run env DOCKER_HOST="$PRIVATE_DOCKER_HOST" DOCKER_CONTEXT=default \
        DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config \
        docker image pull "$HERMES_AGENT_IMAGE"
    user_run env "${ORCHESTRA_ENV[@]}" PYTHONPATH="${REPO}/scripts" \
        python3 - <<'PY'
from environment_resolution import DefaultEnvironmentResolver, EnvironmentSpec
from sandbox_backend import NestedDaemonSandboxBackend, NestedDockerImageClient
environment = DefaultEnvironmentResolver().resolve(EnvironmentSpec(1, "default-worker"))
client = NestedDockerImageClient(
    docker_host="unix:///opt/orchestra/runtime/sandbox-engine-socket/docker.sock"
)
prepared = NestedDaemonSandboxBackend.for_dedicated_nested_daemon(client).materialize(environment)
if prepared.image_reference != environment.image_reference:
    raise SystemExit("Accepted worker authority changed during materialization")
print("ORCHESTRA_DEFAULT_WORKER_MATERIALIZATION_PASS")
PY
    user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-compose.sh" up -d
    user_run env "${ORCHESTRA_ENV[@]}" \
        "${REPO}/scripts/orchestra-controller-probe.py" \
        --base-url http://127.0.0.1:8765 --wait-seconds 30
    user_run "${REPO}/scripts/orchestra-console-probe.py" \
        --base-url http://127.0.0.1:8788 --wait-seconds 30
    if [[ -f "${ORCHESTRA_ROOT}/state/hermes-home/auth.json" ]]; then
        user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/scripts/orchestra-roles.py" verify-profiles
        user_run env "${ORCHESTRA_ENV[@]}" "${REPO}/validate.sh" --runtime
    else
        echo "ATTENTION: auth.json absent; les objectifs IA ne fonctionneront pas encore."
    fi
fi

printf 'FINISHED_SUCCESS\n' >"$STATUS"
trap - ERR
echo "ORCHESTRA_INSTALL_PASS"
echo "Rapport : $REPORT"
echo "Statut  : $STATUS"
